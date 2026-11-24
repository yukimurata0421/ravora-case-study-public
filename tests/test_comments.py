from contextlib import contextmanager
from dataclasses import replace
import datetime as dt
import json
import io
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ravora.comments import Comment,CommentInbox,normalize_youtube,fixture_page
from ravora.conversation import MemoryStore,ConversationBridge,SpeechController
from ravora.input_pipeline import InputPipeline
from ravora.providers import ProviderError,Transport
from ravora.safety import TurnFence
from ravora.settings import ProviderSettings, load_settings
from ravora.youtube import YouTubeHTTP,YouTubeReader
from support import Text,Reaction,Speech


NOW=1780000000.
CHAT='test-live-chat'

def message(mid='m1',author='channel-one',text='Hello',at=NOW):
    return {'id':mid,'snippet':{'liveChatId':CHAT,'type':'textMessageEvent','authorChannelId':author,'publishedAt':dt.datetime.fromtimestamp(at,dt.timezone.utc).isoformat(),'textMessageDetails':{'messageText':text}},'authorDetails':{'channelId':author,'displayName':'Same name'}}

def page(items,token='next',interval=5000,**kwargs):return {'items':items,'nextPageToken':token,'pollingIntervalMillis':interval,**kwargs}

class Stop:
    def __init__(self):self.delays=[];self.stopped=False
    def is_set(self):return self.stopped
    def wait(self,delay):self.delays.append(delay);return False

class FakeYouTube:
    def __init__(self,replies):self.replies=iter(replies);self.calls=[]
    def get(self,resource,params):
        self.calls.append((resource,dict(params)));value=next(self.replies)
        if isinstance(value,Exception):raise value
        return value

    @contextmanager
    def stream(self,settings,chat,cursor,timeout,stop):
        self.calls.append(('streamList',{'pageToken':cursor}))
        def batches():
            for value in self.replies:
                if isinstance(value,Exception):raise value
                yield value
        yield batches()


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=MemoryStore(self.root/'state.sqlite3');self.now=NOW
        self.inbox=CommentInbox(self.store,clock=lambda:self.now,capacity=2,ttl=45)
    def tearDown(self):self.tmp.cleanup()

    def test_normalize_uses_channel_identity_not_display_name(self):
        a=normalize_youtube(message(),CHAT);b=normalize_youtube(message('m2','other-channel'),CHAT)
        self.assertNotEqual(a.viewer,b.viewer);self.assertEqual(a.display_name,b.display_name)
        another_stream=Comment(a.source,'next-stream','new-id',a.author_id,a.display_name,a.text,a.published_at)
        self.assertEqual(a.viewer,another_stream.viewer);self.assertNotEqual(a.receipt,another_stream.receipt)
        with self.assertRaises(ValueError):normalize_youtube(message(), 'wrong-chat')
        invalid=message();invalid['authorDetails']['channelId']='wrong-author'
        with self.assertRaises(ValueError):normalize_youtube(invalid,CHAT)

    def test_duplicate_survives_database_reopen(self):
        self.assertEqual(self.inbox.accept_page(CHAT,page([message(),message()]))['counts'],{'queued':1,'duplicate':1})
        other=CommentInbox(MemoryStore(self.store.path),clock=lambda:NOW)
        self.assertEqual(other.accept_page(CHAT,page([message()]))['counts'],{'duplicate':1})
        self.assertIsNone(other.take())  # A later session never replays another session's work.

    def test_queue_capacity_viewer_fairness_age_and_expiry(self):
        values=[message(),message('same-author'),message('m2','channel-two'),message('overflow','third'),message('old','old',at=NOW-60),message('future','future',at=NOW+40)]
        report=self.inbox.accept_page(CHAT,page(values));self.assertEqual(sum(report['counts'].values()),len(values))
        self.assertEqual(self.inbox.summary(),{'dropped':3,'expired':1,'queued':2})
        self.now+=46;self.assertIsNone(self.inbox.take());self.assertEqual(self.inbox.summary()['expired'],3)

    def test_startup_history_skipped_and_non_text_ignored(self):
        system={'id':'system','snippet':{'type':'newSponsorEvent','liveChatId':CHAT}}
        result=self.inbox.accept_page(CHAT,page([message(at=NOW-1),message('fresh','other',at=NOW),system]),warm_start=NOW)
        self.assertEqual(result['counts'],{'dropped':1,'queued':1,'ignored_type':1})

    def test_invalid_page_does_not_advance_cursor(self):
        self.inbox.accept_page(CHAT,page([],'before'))
        with self.assertRaises(ValueError):self.inbox.accept_page(CHAT,{'items':[message()],'nextPageToken':'after'})
        self.assertEqual(self.inbox.cursor(CHAT),'before')

    def test_tombstone_redacts_and_does_not_reintroduce_late_text(self):
        self.inbox.accept_page(CHAT,page([message()]));c=self.inbox.take();self.store.start('turn',c['viewer'],'Hello');self.inbox.bind(c['receipt'],'turn')
        deleted={'id':'m1','snippet':{'type':'tombstone','liveChatId':CHAT,'publishedAt':dt.datetime.fromtimestamp(NOW,dt.timezone.utc).isoformat()}}
        self.inbox.accept_page(CHAT,page([deleted]));self.store.finish('turn','late response','completed',{})
        self.assertTrue(self.inbox.removed_turn('turn'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT comment,reply,played,status FROM conversation_turns WHERE id=?',('turn',)).fetchone(),('','',0,'source_removed'))
        with self.assertRaises(ValueError):self.inbox.claimed(c['receipt'])

    def test_chat_end_event_and_ban_remove_pending_author(self):
        self.inbox.accept_page(CHAT,page([message()]))
        ban={'id':'ban','snippet':{'type':'userBannedEvent','liveChatId':CHAT,'userBannedDetails':{'bannedUserDetails':{'channelId':'channel-one'}}}}
        end={'id':'end','snippet':{'type':'chatEndedEvent','liveChatId':CHAT}}
        result=self.inbox.accept_page(CHAT,page([ban,end]));self.assertTrue(result['ended']);self.assertIsNone(self.inbox.take())

    def test_retention_redacts_history_but_keeps_turn_status_record(self):
        self.inbox.accept_page(CHAT,page([message()]));c=self.inbox.take();self.store.start('old',c['viewer'],'old text');self.inbox.bind(c['receipt'],'old');self.store.finish('old','old reply','completed',{});self.store.played('old')
        CommentInbox(self.store,clock=lambda:NOW+86401)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT comment,reply,status FROM conversation_turns WHERE id=?',('old',)).fetchone(),('','','source_expired'))

    def test_reader_resolves_video_and_receives_two_batches_on_one_connection(self):
        settings=replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='not-real',youtube_video_id='abcdefghijk')
        http=FakeYouTube([{'items':[{'id':'abcdefghijk','liveStreamingDetails':{'activeLiveChatId':CHAT}}]},page([message()],'p1',12000),page([message(),message('m2','other')],'p2',offlineAt='ended')])
        stop=Stop();reader=YouTubeReader(settings,self.inbox,transport=http,stream=http.stream,clock=lambda:NOW);reader.run(stop,seconds=30)
        self.assertEqual(reader.status,'ended');self.assertEqual(reader.requests,2)
        self.assertEqual([name for name,_ in http.calls],['videos','streamList'])
        self.assertEqual(stop.delays,[])
        self.assertEqual(reader.counts['duplicate'],1)
        self.assertEqual(self.inbox.cursor(CHAT,streaming=True),'p2')

    def test_reader_can_wait_for_studio_to_activate_the_target_chat(self):
        settings=replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='fixture',youtube_video_id='abcdefghijk')
        http=FakeYouTube([{'items':[{'id':'abcdefghijk','liveStreamingDetails':{}}]},
            {'items':[{'id':'abcdefghijk','liveStreamingDetails':{'activeLiveChatId':CHAT}}]},
            page([],offlineAt='ended')])
        reader=YouTubeReader(settings,self.inbox,transport=http,stream=http.stream,clock=lambda:NOW)
        reader.run(Stop(),seconds=30,wait_for_chat=True)
        self.assertEqual((reader.status,reader.requests),('ended',3))

    def test_reader_disabled_never_touches_transport(self):
        http=FakeYouTube([]);r=YouTubeReader(ProviderSettings(),self.inbox,transport=http,stream=http.stream);r.run(Stop());self.assertEqual(r.status,'held');self.assertEqual(http.calls,[])
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('NETWORK_CALLED')):
            with self.assertRaisesRegex(ProviderError,'YOUTUBE_READ_DISABLED'):YouTubeHTTP(ProviderSettings()).get('videos',{})

    def test_reader_target_mismatch_and_quota_are_terminal(self):
        s=replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='not-real',youtube_video_id='abcdefghijk',youtube_chat_id=CHAT)
        http=FakeYouTube([{'items':[{'id':'abcdefghijk','liveStreamingDetails':{'activeLiveChatId':'wrong'}}]}]);r=YouTubeReader(s,self.inbox,transport=http,stream=http.stream);r.run(Stop());self.assertEqual(r.code,'YOUTUBE_TARGET_CHANGED');self.assertEqual(len(http.calls),1)
        http=FakeYouTube([ProviderError('YOUTUBE_GRPC_RESOURCE_EXHAUSTED')]);r=YouTubeReader(replace(s,youtube_video_id=''),self.inbox,transport=http,stream=http.stream);r.run(Stop());self.assertEqual(r.status,'failed');self.assertEqual(len(http.calls),1)

    def test_transient_stream_errors_resume_from_committed_cursor_with_backoff(self):
        s=replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='not-real',youtube_chat_id=CHAT)
        http=FakeYouTube([page([],'resume',9000),ProviderError('YOUTUBE_GRPC_UNAVAILABLE'),page([],'done',offlineAt='ended')]);stop=Stop()
        r=YouTubeReader(s,self.inbox,transport=http,stream=http.stream,clock=lambda:NOW);r.run(stop,seconds=40)
        self.assertEqual(r.status,'ended');self.assertEqual(http.calls[1][1]['pageToken'],'resume');self.assertEqual(len(http.calls),2);self.assertGreaterEqual(stop.delays[0],s.youtube_poll_seconds)

    def test_youtube_http_builds_only_read_requests_and_redacts_errors(self):
        s=replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='private-test-key')
        client=YouTubeHTTP(s)
        with patch.object(client.opener,'open',return_value=io.BytesIO(b'{"items":[]}')) as opened:
            self.assertEqual(client.get('videos',{'id':'abcdefghijk'}),{'items':[]})
            request=opened.call_args.args[0];self.assertEqual(request.get_method(),'GET')
            query=urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            self.assertEqual(query['key'],['private-test-key']);self.assertEqual(query['id'],['abcdefghijk'])
        body=json.dumps({'error':{'message':'private-test-key','errors':[{'reason':'rateLimitExceeded'}]}}).encode()
        error=urllib.error.HTTPError('https://example.invalid',429,'Rate limit',{'Retry-After':'12'},io.BytesIO(body))
        with patch.object(client.opener,'open',side_effect=error):
            with self.assertRaises(ProviderError) as caught:client.get('videos',{})
            self.assertNotIn('private-test-key',str(caught.exception));self.assertEqual(caught.exception.retry_after,12)
        with self.assertRaisesRegex(ProviderError,'INVALID_YOUTUBE_RESOURCE'):client.get('liveChat/messages/insert',{})

    def test_clean_eof_reconnects_only_within_budget(self):
        s=replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='fixture',youtube_chat_id=CHAT,youtube_max_requests=1)
        http=FakeYouTube([page([])]);stop=Stop();reader=YouTubeReader(s,self.inbox,transport=http,stream=http.stream)
        reader.run(stop)
        self.assertEqual(reader.code,'YOUTUBE_REQUEST_LIMIT');self.assertEqual(len(http.calls),1)
        self.assertGreaterEqual(stop.delays[0],s.youtube_poll_seconds)

    def test_continuous_reader_survives_idle_closures_on_the_same_cursor(self):
        s = replace(ProviderSettings(), youtube_read_enabled=True, youtube_key='fixture', youtube_chat_id=CHAT)
        cursors = []
        @contextmanager
        def stream(settings, chat, cursor, timeout, stop):
            cursors.append(cursor)
            yield [page([], 'p')] if len(cursors) <= 6 else [page([message()], 'p2', offlineAt='done')]
        reader = YouTubeReader(s, self.inbox, stream=stream, clock=lambda: NOW)
        stop = Stop()
        reader.run(stop, seconds=None)
        self.assertEqual(cursors, [None] + ['p'] * 6)
        self.assertEqual(stop.delays, [s.youtube_poll_seconds] * 6)
        self.assertEqual((reader.status, reader.counts), ('ended', {'queued': 1}))

    def test_connection_diagnostics_and_distinct_retry_policies(self):
        settings = replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='fixture',youtube_chat_id=CHAT)
        transient = ProviderError('YOUTUBE_GRPC_UNAVAILABLE')
        throttle = ProviderError('YOUTUBE_GRPC_RESOURCE_EXHAUSTED')
        throttle.retry_after = 90
        outcomes = iter([transient, transient, None, transient, None, throttle, throttle,
                         ProviderError('YOUTUBE_GRPC_PERMISSION_DENIED')])
        @contextmanager
        def stream(s, chat, cursor, timeout, stop):
            outcome = next(outcomes)
            if outcome:
                raise outcome
            yield [page([], 'private-cursor')]
        reader = YouTubeReader(settings,self.inbox,stream=stream,clock=lambda:NOW)
        stop = Stop()
        records = []
        def wait(delay):
            snapshot = reader.snapshot()
            records.append((delay,snapshot['last_connection']['reason'],snapshot['last_connection']['batches']))
            self.assertEqual(snapshot['retry_at'],NOW+delay)
            self.assertGreaterEqual(snapshot['last_connection']['duration_seconds'],0)
        stop.wait = wait
        with self.assertLogs('ravora.youtube',level='INFO') as messages:
            reader.run(stop,seconds=None)
        self.assertEqual(records,[(5,str(transient),0),(10,str(transient),0),(5,'eof',1),
                                  (5,str(transient),0),(5,'eof',1),(90,str(throttle),0),(120,str(throttle),0)])
        self.assertEqual(reader.status,'failed')
        self.assertIsNone(reader.snapshot()['retry_at'])
        self.assertEqual(reader.last_connection['reason'],'YOUTUBE_GRPC_PERMISSION_DENIED')
        self.assertEqual(sum('YOUTUBE_CHAT_CONNECTION' in line for line in messages.output),8)
        self.assertNotIn('private-cursor',' '.join(messages.output))
        self.assertNotIn(CHAT,' '.join(messages.output))

    def test_continuous_connection_budget_survives_restart_and_expires_on_a_rolling_hour(self):
        s = replace(ProviderSettings(), youtube_read_enabled=True, youtube_key='fixture', youtube_chat_id=CHAT,
                    youtube_max_requests=2)
        clock = [NOW]
        budget_waits = []
        def run():
            reader = YouTubeReader(s, self.inbox, stream=FakeYouTube([page([], offlineAt='done')]).stream,
                                   clock=lambda: clock[0])
            stop = Stop()
            def wait(delay):
                budget_waits.append((reader.status, delay))
                clock[0] += delay
                return False
            stop.wait = wait
            reader.run(stop, seconds=None)
            self.assertEqual(reader.status, 'ended')
        run()
        clock[0] += 20
        run()
        run()
        self.assertEqual(budget_waits, [('waiting_for_request_budget', 3580)])
        with self.store.connect() as db:
            slots = db.execute('SELECT reserved_at FROM youtube_chat_connections ORDER BY reserved_at').fetchall()
        self.assertEqual(slots, [(NOW + 20,), (NOW + 3600,)])

    def test_live_defaults_and_four_hour_run_with_quiet_and_busy_periods(self):
        env = self.root/'settings.env'
        env.write_text('YOUTUBE_MAX_REQUESTS=40\n')
        env.chmod(0o600)
        settings = replace(load_settings(env, live_studio=True, environ={}), youtube_read_enabled=True,
                           youtube_key='fixture', youtube_chat_id=CHAT)
        self.assertEqual((settings.youtube_chat_requests_per_hour, settings.youtube_chat_daily_units), (240, 4800))
        self.assertIsNone(load_settings(env, environ={}).youtube_chat_requests_per_hour)
        # Start at noon Pacific so all four hours use one quota day.
        self.now = dt.datetime(2026, 11, 23, 20, tzinfo=dt.timezone.utc).timestamp()
        start = self.now
        opens = []
        posted = []
        delays = []
        next_post = start + 1
        @contextmanager
        def stream(s, chat, cursor, timeout, stop):
            nonlocal next_post
            i = len(opens)
            self.assertEqual(cursor, str(i) if i else None)
            self.assertAlmostEqual(timeout, start + 14400 - self.now)
            opens.append(self.now)
            self.now += min(timeout, 10.7)
            items = []
            # A comment every 20s in the busy half of each hour, including while disconnected.
            while next_post <= self.now:
                if (next_post - start) % 3600 < 1800:
                    mid = str(len(posted))
                    items.append(message(mid, mid, at=next_post))
                    posted.append(self.now - next_post)
                next_post += 20
            yield [page(items, str(i + 1), interval=None)]
        def consume_page():
            selected = self.inbox.take()
            if selected:
                self.inbox.finish(selected['receipt'],'cancelled','fixture_consumer_done')
        reader = YouTubeReader(settings, self.inbox, stream=stream, clock=lambda:self.now, on_page=consume_page)
        stop = Stop()
        def wait(seconds):
            delays.append(seconds)
            self.now += seconds
            return False
        stop.wait = wait
        with patch('ravora.youtube.time.monotonic', lambda:self.now):
            reader.run(stop, seconds=14400)
        self.assertEqual((reader.status, reader.code, self.now-start), ('stopped', None, 14400))
        self.assertEqual(reader.counts, {'queued': len(posted)})
        self.assertEqual(len(posted), 360)
        self.assertGreater(max(posted), 10.7)
        self.assertLess(max(posted), self.inbox.ttl)
        self.assertTrue({5,10,15}.issubset(delays))
        self.assertGreater(len(opens), 160)
        self.assertTrue(all(b-a >= 15 for a,b in zip(opens,opens[1:])))
        self.assertEqual(reader.daily_units, len(opens)*5)
        self.assertLessEqual(reader.daily_units, 4800)

    def test_server_wait_survives_transient_error_and_short_eof_circuit(self):
        settings = replace(ProviderSettings(), youtube_read_enabled=True, youtube_key='fixture',
                           youtube_chat_id=CHAT, youtube_chat_requests_per_hour=240)
        @contextmanager
        def stream(s, chat, cursor, timeout, stop):
            def batches():
                if reader.stream_opens == 1:
                    yield page([], 'p1', interval=48000)
                    raise ProviderError('YOUTUBE_GRPC_UNAVAILABLE')
                if reader.stream_opens == 5:
                    yield page([], offlineAt='ended')
                else:
                    yield page([], interval=None)
            yield batches()
        reader = YouTubeReader(settings, self.inbox, stream=stream, clock=lambda:self.now)
        waits = []
        stop = Stop()
        def wait(seconds):
            waits.append((reader.status,seconds))
            self.now += seconds
            return False
        stop.wait = wait
        with patch('ravora.youtube.time.monotonic', lambda:self.now):
            reader.run(stop, seconds=3600)
        self.assertEqual(waits[0], ('reconnecting',48))
        self.assertIn(('short_stream_cooldown',60), waits)
        self.assertEqual((reader.status, reader.daily_units), ('ended',25))

    def test_daily_reservation_across_targets_restart_and_pacific_midnight(self):
        settings = replace(ProviderSettings(), youtube_read_enabled=True, youtube_key='fixture',
                           youtube_chat_id=CHAT, youtube_chat_requests_per_hour=240, youtube_chat_daily_units=10)
        # US daylight saving time has ended on this 25-hour quota day.
        self.now = dt.datetime(2026, 11, 2, 7, 59, 40, tzinfo=dt.timezone.utc).timestamp()
        with self.store.connect() as db:
            db.execute('INSERT INTO youtube_chat_connections VALUES (?,?)', ('previous-release',self.now-60))
        def run(target, seconds, stop):
            s = replace(settings, youtube_chat_id=target)
            reader = YouTubeReader(s,self.inbox,stream=FakeYouTube([page([], offlineAt='end')]).stream,clock=lambda:self.now)
            reader.run(stop, seconds=seconds)
            return reader
        stop = Stop()
        stop.wait = lambda delay: self.advance_clock(delay)
        with patch('ravora.youtube.time.monotonic', lambda:self.now):
            first = run(CHAT, 100, stop)
            limited = run('another-chat', 8, stop)
            tomorrow = run('another-chat', 60, stop)
        self.assertEqual((first.requests,first.daily_units), (1,10))
        self.assertEqual((limited.requests,limited.status,limited.code), (0,'stopped',None))
        self.assertEqual((tomorrow.requests,tomorrow.daily_units), (1,5))
        with self.store.connect() as db:
            days = db.execute('SELECT period_start,reserved_units FROM youtube_chat_daily_usage ORDER BY period_start').fetchall()
        self.assertEqual(days[1][0] - days[0][0], 25*3600)

    def advance_clock(self, delay):
        self.now += delay
        return False

    def test_two_readers_on_different_chats_cannot_spend_the_last_units_twice(self):
        gate = threading.Barrier(2)
        def reserve(target):
            settings = replace(ProviderSettings(), youtube_chat_id=target, youtube_chat_requests_per_hour=240,
                               youtube_chat_daily_units=5)
            reader = YouTubeReader(settings, self.inbox, clock=lambda:self.now)
            gate.wait(timeout=5)
            return reader._request_slot(5)[0]
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(reserve, ('first-chat','second-chat')))
        self.assertCountEqual(results, [None,'waiting_for_daily_budget'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT reserved_units FROM youtube_chat_daily_usage').fetchall(), [(5,)])
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_chat_connections').fetchone(), (1,))

    def test_restarted_reader_enforces_spacing_and_charges_video_lookup_once(self):
        settings = replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='fixture',youtube_chat_id=CHAT,
                           youtube_chat_requests_per_hour=240)
        responses = FakeYouTube([{'items':[{'id':'abcdefghijk','liveStreamingDetails':{'activeLiveChatId':CHAT}}]},
                                 page([],offlineAt='end')])
        settings = replace(settings, youtube_video_id='abcdefghijk')
        reader = YouTubeReader(settings,self.inbox,transport=responses,stream=responses.stream,clock=lambda:self.now)
        waits = []
        stop = Stop()
        def wait(delay):
            waits.append(delay)
            return self.advance_clock(delay)
        stop.wait = wait
        with patch('ravora.youtube.time.monotonic',lambda:self.now):
            reader.run(stop,seconds=100)
            restarted = YouTubeReader(settings,self.inbox,transport=FakeYouTube([]),clock=lambda:self.now)
            restarted.run(stop,seconds=9)
        self.assertEqual((reader.daily_units,reader.status), (6,'ended'))
        self.assertEqual((restarted.daily_units,restarted.requests,restarted.status), (6,0,'stopped'))
        self.assertEqual(waits, [15,9])

    def test_wrong_chat_page_rolls_back_messages_and_checkpoint(self):
        wrong=message('wrong','other');wrong['snippet']['liveChatId']='other-chat'
        with self.assertRaises(ValueError):self.inbox.accept_page(CHAT,page([message(),wrong]))
        self.assertEqual(self.inbox.summary(),{});self.assertIsNone(self.inbox.cursor(CHAT))

    def test_cancelled_reader_never_opens_a_connection(self):
        s=replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='not-real',youtube_chat_id=CHAT)
        stop=threading.Event();stop.set();http=FakeYouTube([]);r=YouTubeReader(s,self.inbox,transport=http,stream=http.stream);r.run(stop)
        self.assertEqual(r.status,'stopped');self.assertEqual(http.calls,[])

    def test_disabled_or_ended_precondition_stops_without_retry(self):
        s=replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='not-real',youtube_chat_id=CHAT)
        http=FakeYouTube([ProviderError('YOUTUBE_GRPC_FAILED_PRECONDITION')]);r=YouTubeReader(s,self.inbox,transport=http,stream=http.stream);r.run(Stop())
        self.assertEqual((r.status,r.code,len(http.calls)),('failed','YOUTUBE_GRPC_FAILED_PRECONDITION',1))

    def test_unexpected_input_failure_is_reported_instead_of_silent_completion(self):
        s=replace(ProviderSettings(),youtube_read_enabled=True,youtube_key='not-real',youtube_chat_id=CHAT)
        http=FakeYouTube([RuntimeError('unexpected local failure')]);reader=YouTubeReader(s,self.inbox,transport=http,stream=http.stream);reader.run(Stop())
        self.assertEqual(reader.status,'failed');self.assertEqual(reader.code,'INVALID_YOUTUBE_RESPONSE')

    def test_provider_send_switch_blocks_even_configured_credentials(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('NETWORK_CALLED')):
            with self.assertRaisesRegex(ProviderError,'API_DISABLED'):Transport().open('https://generativelanguage.googleapis.com/v1beta/models',{})
        s=replace(ProviderSettings(),mode='cloud',api_enabled=False)
        class Forbidden:
            def stream(self,*_):raise AssertionError('PROVIDER_CALLED')
            def classify(self,*_):raise AssertionError('PROVIDER_CALLED')
            def create(self,*_):raise AssertionError('PROVIDER_CALLED')
        events=[];f=Forbidden();SpeechController(s,self.store,self.root,providers=(f,f,f)).run('Hi','viewer','held',events.append,threading.Event())
        self.assertEqual(events[-1]['kind'],'held');self.assertEqual(events[-1]['metrics']['provider_requests'],0)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state,attempted FROM provider_calls').fetchall(),[('held',0),('held',0)])

    def test_pipeline_reads_normalized_receipts_and_updates_after_playback(self):
        s=replace(ProviderSettings(),comment_source='youtube-fixture')
        bridge=ConversationBridge(s,self.store,self.root,TurnFence(),threading.Lock(),providers=(Text(),Reaction(),Speech()));pipeline=InputPipeline(s,self.store,bridge,[]);pipeline.start(30)
        try:
            comment=pipeline.next()['comment'];token=pipeline.begin(comment['receipt']);bridge.worker.join(3)
            self.assertFalse(pipeline.played(token))
            for e in bridge.poll(0)['events']:
                if e['kind']=='audio':self.assertTrue(bridge.claim(token,e['index']))
            self.assertTrue(pipeline.played(token));self.assertEqual(pipeline.inbox.summary()['played'],1)
            with self.store.connect() as db:self.assertTrue(all(row==('accepted',0) for row in db.execute('SELECT state,attempted FROM provider_calls')))
        finally:pipeline.close()

    def test_payment_failure_pauses_following_comments_without_retry(self):
        reacted=threading.Event()
        class Failed(Text):
            def stream(self,*_):
                reacted.wait(1)
                raise ProviderError('PROVIDER_HTTP_402')
        class ReadyReaction(Reaction):
            def classify(self,comment):
                result=super().classify(comment);reacted.set();return result
        s=replace(ProviderSettings(),mode='cloud',api_enabled=True,gemini_key='not-real',jev_key='not-real',tts_key='not-real',comment_source='youtube-fixture')
        bridge=ConversationBridge(s,self.store,self.root,TurnFence(),threading.Lock(),providers=(Failed(),ReadyReaction(),Speech()));pipeline=InputPipeline(s,self.store,bridge,[]);pipeline.start(30)
        try:
            comment=pipeline.next()['comment'];pipeline.begin(comment['receipt']);bridge.worker.join(3)
            value=pipeline.next();self.assertTrue(value['held']);self.assertEqual(value['code'],'PROVIDER_HTTP_402');self.assertEqual(pipeline.inbox.requests_attempted(),2)
            with self.store.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='gemini'").fetchone()[0],1)
        finally:pipeline.close()

    def test_deleted_inflight_comment_discards_late_audio(self):
        entered=threading.Event();release=threading.Event()
        class BlockingSpeech(Speech):
            def create(self,text,output):
                entered.set();release.wait(2);return super().create(text,output)
        s=replace(ProviderSettings(),comment_source='youtube-fixture')
        bridge=ConversationBridge(s,self.store,self.root,TurnFence(),threading.Lock(),providers=(Text(),Reaction(),BlockingSpeech()));pipeline=InputPipeline(s,self.store,bridge,[]);pipeline.start(30)
        try:
            comment=pipeline.next()['comment'];pipeline.begin(comment['receipt']);self.assertTrue(entered.wait(2))
            chat='fixture-'+pipeline.inbox.session
            deleted={'id':'fixture-0','snippet':{'type':'tombstone','liveChatId':chat}}
            pipeline.inbox.accept_page(chat,page([deleted]),source='youtube-fixture');pipeline.check_removed();release.set();bridge.worker.join(3)
            events=bridge.poll(0)['events'];self.assertTrue(any(e['kind']=='cancelled' for e in events));self.assertFalse(any(e['kind']=='audio' for e in events))
            with self.store.connect() as db:self.assertEqual(db.execute("SELECT state FROM provider_calls WHERE provider='tts'").fetchone()[0],'discarded_after_cancel')
        finally:release.set();pipeline.close()

    def test_unknown_outcome_is_preserved_without_accepting_audio(self):
        class Failed(Text):
            def stream(self,*_):raise ProviderError('PROVIDER_CONNECTION_FAILED')
        events=[];SpeechController(ProviderSettings(),self.store,self.root,providers=(Failed(),Reaction(),Speech())).run('Hello','v','unknown',events.append,threading.Event())
        self.assertFalse(any(e['kind']=='audio' for e in events))
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT state FROM provider_calls WHERE provider='gemini'").fetchone()[0],'outcome_unknown')


if __name__=='__main__':unittest.main()
