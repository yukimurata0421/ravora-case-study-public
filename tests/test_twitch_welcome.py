from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    import requests

    from ravora.database import Database
    from ravora.twitch_chat import MESSAGE, TwitchReader
    from ravora.twitch_schedule import Slot
    from ravora.twitch_schedule_api import HELIX, PIN_SCOPES
    from ravora.twitch_welcome import ScheduledWelcome, WELCOME
except ModuleNotFoundError as exc:
    if exc.name not in ('requests', 'websocket'):
        raise
    ScheduledWelcome = None


def response(status, data=None):
    result = Mock(status_code=status)
    result.json.return_value = {'data':data}
    return result


@unittest.skipUnless(ScheduledWelcome, 'youtube-live extra required')
class WelcomeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict('os.environ', RAVORA_TWITCH_CHANNEL='fixture_broadcaster'))
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.db = Database(self.folder/'fixture.sqlite3')
        self.slot = Slot(1000,'ja-19')
        self.stop = Event()
        self.welcome = self.controller()
        self.enterContext(patch('ravora.twitch_welcome.time.time', return_value=self.slot.start+4))
        with self.db.connect() as db:
            db.execute("""INSERT INTO studio_sessions(session,platform,mode,sample_rate,fps,prefetch,chat_source,started,stream_id)
                VALUES('s','twitch','cloud',48000,30,1,'twitch',1001,'10')""")
            db.execute("""INSERT INTO twitch_schedule_runs(scheduled_start,arm,claimed_at,result,session)
                VALUES(1000,'ja-19',1000,'claimed','s')""")
        oauth = SimpleNamespace(access='token',client_id='app',user_id='42')
        self.grant = self.enterContext(patch('ravora.twitch_welcome.channel_grant',return_value=oauth))
        self.http = Mock()
        self.pin = None
        self.shared = []
        self.live_stream = '10'
        self.http.get.side_effect = self.read
        self.http.post.return_value = response(200,[{'is_sent':True,'message_id':'msg-1'}])
        self.http.put.side_effect = self.put
        self.enterContext(patch('ravora.twitch_welcome.requests.Session')).return_value.__enter__.return_value = self.http

    def controller(self):
        return ScheduledWelcome(self.db,self.folder,self.slot,'s',self.stop)

    def read(self,url,*,params,**unused):
        if url == HELIX+'streams':
            return response(200,[{'type':'live','user_login':'fixture_broadcaster','user_id':'42','id':self.live_stream}])
        if url == HELIX+'chat/pins':
            self.assertEqual(params,{'broadcaster_id':'42','moderator_id':'42'})
            return response(200,[self.pin] if self.pin else [])
        self.assertEqual(url,HELIX+'shared_chat/session')
        return response(200,self.shared)

    def put(self,url,*,params,**unused):
        self.assertEqual(url,HELIX+'chat/pins')
        self.assertEqual(params,{'broadcaster_id':'42','moderator_id':'42','message_id':'msg-1'})
        self.pin = {'message_id':'msg-1','broadcaster_id':'42','ends_at':None}
        return response(204)

    def test_confirm_once_until_end_and_ignore_only_own_announcement(self):
        inbox = Mock()
        reader = TwitchReader(inbox,self.folder/'oauth',config=self.folder/'cli',is_announcement=self.welcome.is_announcement)
        stamp = datetime.now(timezone.utc).isoformat()
        def notify(sender,text,mid):
            event = {'broadcaster_user_id':'42','chatter_user_id':sender,'chatter_user_name':'name',
                     'message_id':mid,'message':{'text':text}}
            reader._notification({'subscription_type':MESSAGE,'message_timestamp':stamp},{'event':event},'42')
        def send(url,*,json,**unused):
            self.assertEqual(url,HELIX+'chat/messages')
            self.assertEqual(json,{'sender_id':'42','broadcaster_id':'42','message':WELCOME})
            notify('42',WELCOME,'msg-1')   # EventSub can beat the HTTP response.
            return response(200,[{'is_sent':True,'message_id':'msg-1'}])
        self.http.post.side_effect = send
        self.welcome.on_live('10')
        self.assertEqual(self.welcome._record(),('pinned','msg-1'))
        self.grant.assert_called_once_with(self.folder/'pin-oauth.json',self.http,PIN_SCOPES)
        notify('42',WELCOME,'msg-1')
        notify('42','こんにちは','manual')
        notify('43',WELCOME,'viewer')
        self.assertEqual(inbox.put.call_count,2)
        self.assertEqual(reader.counts['ignored_welcome'],2)
        self.pin = None  # Operator unpinned; even a recreated controller must respect that.
        self.controller().on_live('10')
        self.http.post.assert_called_once()
        self.http.put.assert_called_once()

    def test_existing_pin_and_shared_chat(self):
        self.pin = {'message_id':'another','broadcaster_id':'42','ends_at':None}
        self.welcome.on_live('10')
        self.assertEqual(self.welcome._record()[0],'skipped_existing_pin')
        self.http.post.assert_not_called()
        self.http.put.assert_not_called()

    def test_no_cross_channel_send(self):
        self.shared = [{'session_id':'shared'}]
        self.welcome.on_live('10')
        self.assertEqual(self.welcome._record()[0],'skipped_shared_chat')
        self.http.post.assert_not_called()

    def test_stream_and_local_binding_must_both_match(self):
        self.live_stream = 'previous'
        self.welcome.on_live('10')
        self.live_stream = '10'
        with self.db.connect() as db:
            db.execute("UPDATE studio_sessions SET identity_conflict=1 WHERE session='s'")
        self.welcome.on_live('10')
        self.assertIsNone(self.welcome._record())
        self.http.post.assert_not_called()

    def test_uncertain_post_is_not_repeated(self):
        self.http.post.side_effect = requests.Timeout()
        self.welcome.on_live('10')
        self.assertEqual(self.welcome._record(),('send_unknown',None))
        self.controller().on_live('10')
        self.http.post.assert_called_once()
        self.http.put.assert_not_called()

    def test_lost_put_reply_reconciles_without_resending(self):
        def lose_ack(*args,**kwargs):
            self.put(*args,**kwargs)
            raise requests.Timeout()
        self.http.put.side_effect = lose_ack
        self.welcome.on_live('10')
        self.assertEqual(self.welcome._record()[0],'pin_dispatched')
        self.controller().on_live('10')
        self.assertEqual(self.welcome._record()[0],'pinned')
        self.http.put.assert_called_once()
        self.http.post.assert_called_once()

    def test_moderator_pins_after_send(self):
        def send(*args,**kwargs):
            self.pin = {'message_id':'staff','broadcaster_id':'42','ends_at':None}
            return response(200,[{'is_sent':True,'message_id':'msg-1'}])
        self.http.post.side_effect = send
        self.welcome.on_live('10')
        self.assertEqual(self.welcome._record(),('skipped_existing_pin','msg-1'))
        self.http.put.assert_not_called()

    def test_read_failure_after_send_recovers_only_the_pin(self):
        def interrupted(*args,**kwargs):
            self.http.get.side_effect = requests.ConnectionError()
            return response(200,[{'is_sent':True,'message_id':'msg-1'}])
        self.http.post.side_effect = interrupted
        self.welcome.on_live('10')
        self.assertEqual(self.welcome._record()[0],'sent')
        self.http.get.side_effect = self.read
        self.controller().on_live('10')
        self.assertEqual(self.welcome._record()[0],'pinned')
        self.http.post.assert_called_once()
        self.http.put.assert_called_once()
