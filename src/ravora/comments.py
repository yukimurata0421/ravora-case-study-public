"""Bounded comment inbox for live chat and fixtures. No network in this module."""
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import time
import uuid

from ravora.conversation import MemoryStore
from ravora.developer_identity import viewer_key


VIEWER_CHAT_SOURCES = ('youtube', 'youtube-fixture', 'twitch')


@dataclass(frozen=True)
class Comment:
    source: str
    stream_id: str
    message_id: str
    author_id: str
    display_name: str
    text: str
    published_at: float

    @property
    def receipt(self):
        return hashlib.sha256(json.dumps([self.source,self.stream_id,self.message_id]).encode()).hexdigest()

    @property
    def viewer(self):
        # Same display names do not share memory; fixture/live namespaces stay separate.
        # Channel IDs are stable across streams. Version the key without reassigning old history.
        return viewer_key(self.source, self.author_id)


def timestamp(value):
    if not isinstance(value,str):raise ValueError('INVALID_PUBLISHED_AT')
    parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('PUBLISHED_AT_REQUIRES_TIMEZONE')
    return parsed.timestamp()


def clean(value,limit):
    if not isinstance(value,str) or not value or len(value)>limit or any(ord(c)<32 and c not in '\n\r\t' for c in value):raise ValueError('INVALID_COMMENT_FIELD')
    return ' '.join(value.split())


def normalize_youtube(item,chat_id,source='youtube'):
    if not isinstance(item,dict):raise ValueError('INVALID_YOUTUBE_ITEM')
    snippet=item.get('snippet',{});kind=snippet.get('type')
    if snippet.get('liveChatId')!=chat_id:raise ValueError('YOUTUBE_CHAT_ID_MISMATCH')
    if kind=='tombstone':return {'delete':clean(item.get('id'),1024)}
    if kind=='chatEndedEvent':return {'end':True}
    if kind=='userBannedEvent':return {'ban':clean(snippet.get('userBannedDetails',{}).get('bannedUserDetails',{}).get('channelId'),128)}
    if kind!='textMessageEvent':return None
    author=item.get('authorDetails',{})
    author_id=clean(author.get('channelId') or snippet.get('authorChannelId'),128)
    if snippet.get('authorChannelId') and author_id!=snippet['authorChannelId']:raise ValueError('YOUTUBE_AUTHOR_ID_MISMATCH')
    return Comment(source,chat_id,clean(item.get('id'),1024),author_id,
                   clean(author.get('displayName','Viewer'),100),
                   clean(snippet.get('textMessageDetails',{}).get('messageText'),1000),
                   timestamp(snippet.get('publishedAt')))


class CommentInbox:
    def __init__(self,store:MemoryStore,*,capacity=5,ttl=45,session=None,clock=time.time,scope='offline',relationships_enabled=True):
        if scope not in ('offline','cloud'):raise ValueError('INVALID_RELATIONSHIP_SCOPE')
        self.display_revision=0
        self.removal_revision=0
        self.scope=scope;self.relationships_enabled=relationships_enabled
        self.store=store;self.capacity=capacity;self.ttl=ttl;self.session=session or uuid.uuid4().hex;self.clock=clock
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            store.purge_expired(db,clock())
            # Data minimization for this prototype: keep at most one day of comments.
            db.execute("UPDATE conversation_turns SET comment='',reply='',played=0,status='source_expired' WHERE status!='source_removed' AND id IN (SELECT turn_id FROM comment_inbox WHERE received<?)",(clock()-86400,))
            db.execute('DELETE FROM comment_inbox WHERE received<?',(clock()-86400,))
            db.execute('DELETE FROM youtube_cursors WHERE updated<?',(clock()-86400,))

    def _put(self,db,comment,*,warm_start=None,relationship_evidence=None):
        now=self.clock();status='queued';reason=None
        if db.execute('SELECT 1 FROM source_events WHERE id=?',(comment.receipt,)).fetchone():return 'duplicate'
        if comment.published_at>now+30:status,reason='dropped','future_timestamp'
        elif now-comment.published_at>self.ttl:status,reason='expired','comment_too_old'
        elif warm_start is not None and comment.published_at<warm_start:status,reason='dropped','startup_history'
        elif db.execute("SELECT COUNT(*) FROM comment_inbox WHERE session=? AND status='queued'",(self.session,)).fetchone()[0]>=self.capacity + (comment.source=='live-host'):status,reason='dropped','queue_full'
        elif db.execute("SELECT 1 FROM comment_inbox WHERE session=? AND viewer=? AND status IN ('queued','claimed','processing')",(self.session,comment.viewer)).fetchone():status,reason='dropped','viewer_already_pending'
        row=db.execute('INSERT OR IGNORE INTO comment_inbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (comment.receipt,self.session,comment.source,comment.stream_id,comment.message_id,comment.viewer,comment.display_name,comment.text,comment.published_at,now,min(now+self.ttl,comment.published_at+self.ttl),status,reason,None))
        if row.rowcount:
            self.store.record_source(db,comment,now)
            if self.relationships_enabled and comment.source!='ray-initiative':self.store.record_relationship_comment(db,comment,now,scope=self.scope,reason=reason,evidence=relationship_evidence)
        return status if row.rowcount else 'duplicate'

    def put(self,comment,*,relationship_evidence=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            status=self._put(db,comment,relationship_evidence=relationship_evidence)
        if comment.source in VIEWER_CHAT_SOURCES and status!='duplicate':
            self.display_revision+=1
        return status

    def accept_page(self,chat_id,page,*,source='youtube',warm_start=None,streaming=False):
        token=page.get('nextPageToken');interval=page.get('pollingIntervalMillis')
        if not (streaming and token is None) and (not isinstance(token,str) or not 0<len(token)<=4096):
            raise ValueError('INVALID_YOUTUBE_PAGE')
        if not streaming and (type(interval) is not int or not 0<interval<=3600000):
            raise ValueError('INVALID_YOUTUBE_PAGE')
        items=page.get('items',[])
        if not isinstance(items,list) or len(items)>2000:raise ValueError('INVALID_YOUTUBE_ITEMS')
        counts={};ended=bool(page.get('offlineAt'))
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE comment_inbox SET status='expired',reason='queue_expired' WHERE session=? AND status='queued' AND expires<=?",(self.session,self.clock()))
            for item in items:
                try:comment=normalize_youtube(item,chat_id,source)
                except ValueError as exc:
                    if str(exc)=='YOUTUBE_CHAT_ID_MISMATCH':raise
                    state='invalid'
                except (TypeError,AttributeError):state='invalid'
                else:
                    if isinstance(comment,dict):
                        if 'delete' in comment:self._remove(db,source,chat_id,comment['delete']);state='removed'
                        elif 'ban' in comment:
                            viewer=Comment(source,chat_id,'',comment['ban'],'','',0).viewer
                            legacy=source+':'+hashlib.sha256((chat_id+'\0'+comment['ban']).encode()).hexdigest()[:40]
                            ids=db.execute('SELECT external_event_id FROM source_events WHERE platform=? AND stream_id=? AND viewer_key IN (?,?)',(source,chat_id,viewer,legacy)).fetchall()
                            for (message_id,) in ids:self._remove(db,source,chat_id,message_id)
                            state='banned_author'
                        else:ended=True;state='chat_ended'
                    elif comment is None:state='ignored_type'
                    else:state=self._put(db,comment,warm_start=warm_start)
                counts[state]=counts.get(state,0)+1
            # Checkpoint and accepted messages commit together; failed transactions do not skip a page.
            if token is not None:
                db.execute('INSERT INTO youtube_cursors VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET token=excluded.token,updated=excluded.updated',(self.cursor_key(chat_id,streaming),token,self.clock()))
        if items:
            self.display_revision+=1
        if counts.get('removed') or counts.get('banned_author'):
            self.removal_revision+=1
        poll_after = interval / 1000 if type(interval) is int and 0 < interval <= 0xffffffff else None
        return {'counts':counts,'poll_after_seconds':poll_after,'ended':ended}

    @staticmethod
    def cursor_key(chat_id, streaming):
        # Resume tokens belong to their issuing method. Preserve older list checkpoints.
        return 'streamList\0'+chat_id if streaming else chat_id

    def cursor(self,chat_id,*,streaming=False):
        with self.store.connect() as db:
            row=db.execute('SELECT token FROM youtube_cursors WHERE chat_id=?',(self.cursor_key(chat_id,streaming),)).fetchone()
        return row[0] if row else None

    def _remove(self,db,source,stream_id,message_id):
        event=db.execute('SELECT id FROM source_events WHERE platform=? AND stream_id=? AND external_event_id=?',(source,stream_id,message_id)).fetchone()
        if event:self.store.remove_source(db,event[0],self.clock())
        row=db.execute('SELECT turn_id FROM comment_inbox WHERE source=? AND stream_id=? AND message_id=?',(source,stream_id,message_id)).fetchone()
        db.execute("UPDATE comment_inbox SET status='removed',reason='source_deleted',text='',display_name='' WHERE source=? AND stream_id=? AND message_id=?",(source,stream_id,message_id))
        if row and row[0]:db.execute("UPDATE conversation_turns SET comment='',reply='',played=0,status='source_removed' WHERE id=?",(row[0],))

    def remove(self, source, stream_id, message_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._remove(db, source, stream_id, message_id)
        self.display_revision += 1
        self.removal_revision += 1

    def claim_idle_initiative(self,comment):
        # Check for a human again inside the writer transaction. Never leave a
        # synthetic comment waiting behind a newly arrived human turn.
        now = self.clock()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("""SELECT 1 FROM comment_inbox WHERE session=? AND status='queued'
                    AND expires>? LIMIT 1""",(self.session,now)).fetchone():
                return None
            if self._put(db,comment)!='queued':
                return None
            db.execute("UPDATE comment_inbox SET status='claimed' WHERE receipt=?",(comment.receipt,))
        return dict(receipt=comment.receipt,source=comment.source,display_name=comment.display_name,
                    comment=comment.text,viewer=comment.viewer)

    def human_waiting(self):
        with self.store.connect() as db:
            return bool(db.execute("""SELECT 1 FROM comment_inbox WHERE session=? AND status='queued'
                AND source!='ray-initiative' AND expires>? LIMIT 1""",(self.session,self.clock())).fetchone())

    def take(self, *, prefer_source=None, viewer_only=False):
        now=self.clock()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE comment_inbox SET status='expired',reason='queue_expired' WHERE session=? AND status='queued' AND expires<=?",(self.session,now))
            row=db.execute("SELECT receipt,source,display_name,text,viewer,expires FROM comment_inbox WHERE session=? AND status='queued' ORDER BY (source=?) DESC,received,rowid LIMIT 1",(self.session,prefer_source)).fetchone()
            if not row or (viewer_only and row[1] not in VIEWER_CHAT_SOURCES):return None
            db.execute("UPDATE comment_inbox SET status='claimed' WHERE receipt=?",(row[0],))
        comment = dict(zip(('receipt','source','display_name','comment','viewer'),row))
        if viewer_only:
            comment['expires'] = row[5]
        return comment

    def display_comments(self, selected):
        # Keep the active comment visible when six newer messages arrive.
        with self.store.connect() as db:
            return db.execute("""SELECT receipt,display_name,text FROM (
                SELECT receipt,display_name,text,rowid AS seq FROM comment_inbox
                WHERE session=? AND source IN (?,?,?)
                  AND status!='removed' AND text!=''
                  AND (reason IS NULL OR reason IN ('queue_full','viewer_already_pending','queue_expired'))
                ORDER BY (receipt=?) DESC,rowid DESC LIMIT 6) ORDER BY seq""",
                (self.session,*VIEWER_CHAT_SOURCES,selected)).fetchall()

    def claimed(self,receipt):
        with self.store.connect() as db:
            row=db.execute("SELECT text,viewer FROM comment_inbox WHERE receipt=? AND session=? AND status='claimed' AND expires>?",(receipt,self.session,self.clock())).fetchone()
        if not row:raise ValueError('COMMENT_NO_LONGER_AVAILABLE')
        return row

    def bind(self,receipt,turn_id,*,prefetched=False):
        with self.store.connect() as db:
            row=db.execute("UPDATE comment_inbox SET status='processing',turn_id=? WHERE receipt=? AND session=? AND status='claimed' AND expires>?",(turn_id,receipt,self.session,self.clock()))
            if row.rowcount and prefetched:
                db.execute('UPDATE studio_inputs SET prefetched=1 WHERE receipt=?', (receipt,))
            return row.rowcount==1

    def playable(self, receipt):
        with self.store.connect() as db:
            return bool(db.execute("""SELECT 1 FROM comment_inbox WHERE receipt=? AND session=?
                AND status IN ('processing','generated') AND expires>?""",
                (receipt,self.session,self.clock())).fetchone())

    def finish(self,receipt,status,reason=None):
        with self.store.connect() as db:db.execute("UPDATE comment_inbox SET status=?,reason=? WHERE receipt=? AND session=? AND status NOT IN ('removed','expired','cancelled','played','failed','held')",(status,reason,receipt,self.session))

    def removed_turn(self,turn_id):
        with self.store.connect() as db:return bool(db.execute("SELECT 1 FROM comment_inbox WHERE turn_id=? AND status='removed'",(turn_id,)).fetchone())

    def requests_attempted(self):
        with self.store.connect() as db:
            return db.execute('SELECT COALESCE(SUM(p.attempted),0) FROM provider_calls p JOIN comment_inbox i ON i.turn_id=p.turn_id WHERE i.session=?',(self.session,)).fetchone()[0]

    def summary(self):
        with self.store.connect() as db:rows=db.execute('SELECT status,COUNT(*) FROM comment_inbox WHERE session=? GROUP BY status',(self.session,)).fetchall()
        return dict(rows)

    def close(self):
        with self.store.connect() as db:db.execute("UPDATE comment_inbox SET status='cancelled',reason='session_closed' WHERE session=? AND status IN ('queued','claimed','processing')",(self.session,))


def fixture_page(chat_id,now):
    def item(index,text,author):
        return {'id':'fixture-'+str(index),'snippet':{'type':'textMessageEvent','liveChatId':chat_id,'authorChannelId':author,'publishedAt':dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),'textMessageDetails':{'messageText':text}},'authorDetails':{'channelId':author,'displayName':['Mia','Alex','Sam'][index%3]}}
    first=item(0,'Hello! It is nice to meet you.','fixture-viewer-0')
    return {'nextPageToken':'fixture-complete','pollingIntervalMillis':5000,'items':[first,first,item(1,'What does your perfect afternoon look like?','fixture-viewer-1'),item(2,'Where should I start with a busy day?','fixture-viewer-2')]}
