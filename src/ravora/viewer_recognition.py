"""Names and visit evidence from provider-authenticated chat, in the intake transaction."""
import re
import time
import unicodedata

from ravora.developer_identity import viewer_key


FORGET_NAME = re.compile(r"(?:please )?(?:forget my name|(?:don't|do not|never) (?:save|remember|store) my name)|(?:私の|僕の)?(?:名前|呼び名)(?:は|を)?(?:忘れて|覚えないで|保存しないで)", re.I)
# Only an explicit first-person naming request can create a lasting alias.
NAME_REQUEST = re.compile(r"(?:please call me |call me )([\w-]+(?: [\w-]+){0,2})|(?:私を|僕を)?[「『]?([\w-]+)[」』]?(?:と|って)呼んで(?:ください)?", re.I)


def name_command(text):
    words = unicodedata.normalize('NFKC', text).strip().rstrip('。.!！').strip()
    if FORGET_NAME.fullmatch(words):
        return 'forget', None
    match = NAME_REQUEST.fullmatch(words)
    if match:
        name = match[1] or match[2]
        if len(name) <= 30 and not re.search(r'を|と呼|って呼', name):
            return 'set', name
    return None, None


def names_hidden(db, scope, viewer):
    return db.execute('SELECT 1 FROM viewer_name_optouts WHERE scope=? AND viewer_key=?',
                      (scope, viewer)).fetchone() is not None


def current_name(db, scope, viewer, now, *, cutoff=None):
    args = [scope, viewer, now]
    before = ''
    if cutoff is not None:
        before = ' AND (r.observed_at,r.rowid)<=(?,?)'
        args.extend(cutoff)
    row = db.execute("""SELECT r.display_name FROM relationship_events r JOIN source_events s
        ON s.id=r.source_event_id WHERE r.scope=? AND r.viewer_key=? AND r.eligible=1
        AND s.expires_at>? AND s.is_deleted=0 AND s.raw_purged_at IS NULL
        AND r.display_name IS NOT NULL""" + before + ' ORDER BY r.observed_at DESC,r.rowid DESC LIMIT 1', args).fetchone()
    return row[0] if row else None


def fill_missing_name(db, comment, scope, now):
    if not comment.display_name or names_hidden(db,scope,comment.viewer):
        return
    changed = db.execute("""UPDATE relationship_events SET display_name=?,name_missing=0 WHERE source_event_id=? AND
        scope=? AND viewer_key=? AND eligible=1 AND name_missing=1 AND EXISTS
        (SELECT 1 FROM source_events s WHERE s.id=source_event_id AND s.is_deleted=0
         AND s.raw_purged_at IS NULL AND s.expires_at>?)""",
        (comment.display_name,comment.receipt,scope,comment.viewer,now))
    if changed.rowcount:
        db.execute("UPDATE comment_inbox SET display_name=? WHERE receipt=? AND display_name IN ('','Viewer')",
                   (comment.display_name,comment.receipt))


def observe(db, comment, session, scope):
    viewer = comment.viewer
    event = db.execute('SELECT fresh,eligible FROM relationship_events WHERE source_event_id=?',
                       (comment.receipt,)).fetchone()
    if not event or not event[0]:
        return
    command, preferred = name_command(comment.text)
    if command == 'forget':
        db.execute('INSERT OR IGNORE INTO viewer_name_optouts VALUES(?,?)', (scope,viewer))
        db.execute('UPDATE relationship_events SET display_name=NULL,preferred_name=NULL,name_missing=0 WHERE scope=? AND viewer_key=?',
                   (scope,viewer))
    if not event[1]:
        return
    if command == 'set':
        db.execute('DELETE FROM viewer_name_optouts WHERE scope=? AND viewer_key=?', (scope,viewer))
        db.execute('UPDATE relationship_events SET preferred_name=NULL WHERE scope=? AND viewer_key=? AND preferred_name IS NOT NULL',
                   (scope,viewer))
    hidden = names_hidden(db, scope, viewer)
    db.execute('UPDATE relationship_events SET session=?,display_name=?,preferred_name=?,name_missing=? WHERE source_event_id=?',
               (session, None if hidden else comment.display_name or None, preferred, int(not hidden and not comment.display_name), comment.receipt))


def resolve(db, scope, platform, *, author_id=None, name=None, now=None):
    """Internal lookup. A stale or ambiguous name never assigns a post to an account."""
    if author_id:
        key = viewer_key(platform, author_id)
        row = db.execute('SELECT viewer_key FROM viewer_relationships WHERE scope=? AND viewer_key=? AND eligible_comments>0',
                         (scope,key)).fetchone()
        return row[0] if row else None
    if not name:
        return None
    now = time.time() if now is None else now
    matches = set()
    rows = db.execute("""SELECT DISTINCT viewer_key FROM relationship_events WHERE scope=? AND
        (display_name=? COLLATE NOCASE OR preferred_name=? COLLATE NOCASE) AND eligible=1
        AND substr(viewer_key,1,length(?)+1)=?||':'""", (scope,name,name,platform,platform))
    for (viewer,) in rows:
        alias = db.execute('SELECT preferred_name FROM relationship_events WHERE scope=? AND viewer_key=? AND eligible=1 AND preferred_name=? COLLATE NOCASE',
                           (scope,viewer,name)).fetchone()
        display = current_name(db,scope,viewer,now)
        if alias or (display and display.casefold()==name.casefold()):
            matches.add(viewer)
            if len(matches)>1:
                return None
    return next(iter(matches), None)


def visit(db, platform, stream, session):
    if platform.startswith('youtube') and stream:
        return ('chat', stream)
    if not session:
        return None
    row = db.execute("""SELECT scheduled_start FROM twitch_schedule_runs WHERE session=?
                        UNION ALL SELECT scheduled_start FROM twitch_slot_restarts WHERE session=? LIMIT 1""",
                     (session,session)).fetchone()
    if row:
        return ('slot',row[0])
    row = db.execute("SELECT stream_id FROM studio_sessions WHERE session=? AND identity_conflict=0 AND platform=?",
                     (session,platform)).fetchone()
    return ('stream',row[0]) if row and row[0] else ('session',session)


def turn_context(db, viewer, scope, turn, now):
    anchor = db.execute("""SELECT r.rowid,r.observed_at,r.day,r.session,s.platform,s.stream_id
        FROM conversation_turns t JOIN relationship_events r ON r.source_event_id=t.source_event_id
        JOIN source_events s ON s.id=r.source_event_id WHERE t.turn_id=? AND t.viewer=? AND
        r.scope=? AND r.viewer_key=? AND r.eligible=1 AND s.is_deleted=0 AND s.raw_purged_at IS NULL
        AND s.expires_at>?""",(turn,scope+':'+viewer,scope,viewer,now)).fetchone()
    if not anchor:
        return None
    seq, at, today, session, platform, stream = anchor
    before = 'r.scope=? AND r.viewer_key=? AND r.eligible=1 AND (r.observed_at,r.rowid)<(?,?)'
    args = (scope,viewer,at,seq)
    last = db.execute('SELECT r.day,r.session,s.stream_id FROM relationship_events r JOIN source_events s ON s.id=r.source_event_id WHERE '+
                      before+' ORDER BY r.observed_at DESC,r.rowid DESC LIMIT 1',args).fetchone()
    prior = 'none_recorded'
    pattern = 'first_recorded_contact'
    spoken = False
    familiarity = 'limited_history'
    if last:
        old_day, old_session, old_stream = last
        current_visit = visit(db,platform,stream,session)
        old_visit = visit(db,platform,old_stream,old_session)
        if (session and session == old_session) or (current_visit and current_visit == old_visit):
            prior = 'same_broadcast'
        elif current_visit and old_visit and current_visit[0] == old_visit[0] and current_visit[0] != 'session':
            prior = 'another_broadcast_today' if old_day == today else 'across_prior_days'
        else:
            prior = 'earlier_in_current_day' if old_day == today else 'across_prior_days'
        units,spoken = db.execute('SELECT SUM(min(posts,3)+min(replies,2)),max(replies>0) FROM (SELECT count(*) AS posts,'
            'sum(CASE WHEN r.played_at<=? THEN 1 ELSE 0 END) AS replies FROM relationship_events r WHERE '+before+' GROUP BY r.day)',
            (at,*args)).fetchone()
        familiarity = 'established_contact' if units>=42 else 'recurring_contact' if units>=7 else 'limited_history'
        pattern = 'reconnecting_after_a_gap' if today-old_day>=14 else 'ongoing_contact'
    result = dict(contact_history=familiarity,prior_contact=prior,recent_pattern=pattern,
                  prior_spoken_contact='previously_played' if spoken else 'none_recorded')
    if not names_hidden(db,scope,viewer):
        name = current_name(db,scope,viewer,now,cutoff=(at,seq))
        alias = db.execute("""SELECT preferred_name FROM relationship_events r WHERE scope=? AND viewer_key=?
            AND eligible=1 AND preferred_name IS NOT NULL AND (observed_at,rowid)<=(?,?)
            ORDER BY observed_at DESC,rowid DESC LIMIT 1""",(scope,viewer,at,seq)).fetchone()
        if name:
            result['display_name'] = name
        if alias:
            result['preferred_name'] = alias[0]
    return result
