from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from ravora.comments import Comment, CommentInbox
from ravora.conversation import MemoryStore
from ravora.ray_public import JST, dumps, project
from ravora.twitch_schedule import slot_at
from ravora.studio_observations import StudioObservations, import_observation


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore(Path(self.tmp.name) / 'test.db')

    def broadcast(self, start, viewers=(), platform='twitch', *, coverage=60, end=True, confirmed=True):
        clock = [start]
        inbox = CommentInbox(self.store, scope='cloud', clock=lambda: clock[0])
        stream = f'stream{int(start)}'
        obs = StudioObservations(inbox, platform, False, platform, stream, sample_rate=48000, fps=30)
        with self.store.connect() as db:
            db.execute('UPDATE studio_sessions SET started=? WHERE session=?', (start, inbox.session))
        if confirmed:
            obs.audience(start + 1, 'live', stream, 3, start)
        for offset in range(coverage + 1):
            obs.chat_sample(True, start + offset)
        for n, (who, offset) in enumerate(viewers):
            clock[0] = start + offset
            c = Comment(platform, 'channel', f'{start}-{n}', who, 'PRIVATE NAME', 'PRIVATE TEXT', clock[0])
            inbox.put(c)
            if inbox.take():
                turn = f'{start}-t{n}'
                inbox.bind(c.receipt, turn)
                self.store.start(turn, 'cloud:' + c.viewer, c.text)
                inbox.finish(c.receipt, 'played')
        obs.close(30 * 60, 'stopped')
        with self.store.connect() as db:
            db.execute('UPDATE studio_sessions SET stopped=? WHERE session=?',
                       (start + 60 if end else None, inbox.session))
            db.execute('UPDATE studio_inputs SET played_at=received+2 WHERE session=?', (inbox.session,))
        return inbox.session

    def bind(self, session, start, *, full_length=False):
        slot = slot_at(start)
        with self.store.connect() as db:
            db.execute("INSERT INTO twitch_schedule_runs VALUES(?,?,?,?,'finished',NULL,?)",
                       (slot.start, slot.arm, start, slot.end, session))
            if full_length:
                db.execute('UPDATE studio_sessions SET stopped=?, frames=30*7200 WHERE session=?',(slot.end,session))
                db.execute('DELETE FROM studio_chat_intervals WHERE session=?',(session,))
                db.execute('INSERT INTO studio_chat_intervals VALUES(?,?,?)',(session,start,slot.end))

    def value(self, now=None):
        with self.store.connect() as db:
            return project(db, now if now is not None else time.time())

    def test_projection_contains_counts_only(self):
        sunday = datetime(2026, 11, 29, 19, 1, tzinfo=JST).timestamp()
        tuesday = datetime(2026, 12, 1, 11, 0, tzinfo=JST).timestamp()
        s1 = self.broadcast(sunday, [('alice', 0), ('bob', 10)])
        s2 = self.broadcast(tuesday, [('alice', 5)])
        self.bind(s1, sunday, full_length=True)
        self.bind(s2, tuesday, full_length=True)
        self.broadcast(tuesday + 600, [('carol', 0)], platform='local')  # rehearsal: not public
        with self.store.connect() as db:
            value = project(db, time.time())
        text = dumps(value)
        for secret in ('PRIVATE', 'alice', 'bob', s1, s2):
            self.assertNotIn(secret, text)
        self.assertTrue(value['pipeline']['observations_ready'])
        self.assertEqual([b['slot'] for b in value['broadcasts']], ['en-11', 'ja-19'])
        sun = next(s for s in value['slots'] if s['id'] == 'ja-19')
        self.assertEqual((sun['broadcasts'], sun['speakers']), (1, 2))
        self.assertEqual(sun['cohort'], {'first_seen':2,'replied':2,'spoke_again':0})
        self.assertEqual(value['broadcasts'][0]['cohort']['first_seen'],0)
        self.assertEqual(value['totals']['repeat_viewers'], {'speakers': 2, 'on_two_or_more_days': 1})
        self.assertEqual(value['totals']['broadcasts'], 2)
        json.loads(text)

    def test_missing_partial_and_measured_zero_have_different_denominators(self):
        first = datetime(2026, 11, 25, 20, tzinfo=JST).timestamp()
        self.broadcast(first, coverage=0)
        self.broadcast(first+120, coverage=10)
        short = self.broadcast(first+240, coverage=60)
        self.bind(short, first+240)
        v = self.value(first+86400)
        by_coverage = {r['chat']['status']:r for r in v['broadcasts']}
        self.assertIsNone(by_coverage['unobserved']['speakers'])
        self.assertEqual(by_coverage['partial']['chat']['observed_seconds'],10)
        self.assertEqual(by_coverage['complete']['speakers'],0)
        slot = next(s for s in v['slots'] if s['id']=='ja-19')
        self.assertEqual((slot['broadcasts'],slot['eligible'],slot['speakers']), (1,0,None))
        self.assertEqual([r['slot'] for r in v['broadcasts']].count('off-plan'),2)
        self.assertEqual((v['experiment']['weekly_broadcasts'],v['experiment']['weekly_hours']),(46,92))

    def test_market_missing_partial_and_competitor_intervals(self):
        start = datetime(2026,11,24,17,tzinfo=JST).timestamp()
        session = self.broadcast(start)
        self.bind(session, start, full_length=True)
        stream = f'stream{int(start)}'
        with self.store.connect() as db:
            for tick,status,ray,rival in [(0,'complete',stream,'r1'),(1,'partial',stream,'r1'),
                       (2,'error',stream,None),(3,'complete',stream,None),(5,'complete',stream,None),
                       (6,'partial','different',None),(24,'complete',stream,'r1')]:
                db.execute('INSERT INTO twitch_schedule_market VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (start,tick,start+tick*300,status,1,10,100,40,start+tick*300,ray,rival))
            v = project(db,start+7200)
        row, = v['broadcasts']
        m = row['market']
        self.assertEqual((m['expected_samples'],m['complete_samples'],m['partial_samples'],m['failed_samples']), (24,3,2,1))
        self.assertEqual((m['avg_channels'],m['avg_viewers'],m['top5_share_pct']), (10,100,40))
        self.assertEqual((m['overlap_minutes'],m['paired_ray_minutes']), (5,10))
        self.assertEqual(next(s['market'] for s in v['slots'] if s['id']=='ja-17'), m)
        self.assertNotIn(stream,dumps(v))

    def test_crash_summary_and_expiring_provider_evidence(self):
        start = time.time()-86400
        sid = self.broadcast(start, end=False)
        self.assertEqual(self.value(start+20)['broadcasts'][0]['state'], 'live')
        self.assertEqual(self.value(start+151.01)['broadcasts'][0]['state'], 'unknown')
        with self.store.connect() as db:
            import_observation(db,dict(kind='summary', platform='twitch', stream_id=f'stream{int(start)}',
                session=sid, started_at=start, ended_at=start+3600, unique_viewers=0, peak_viewers=8))
        v = self.value()
        self.assertEqual((v['broadcasts'][0]['state_source'],v['totals']['broadcasts']), ('summary',1))
        self.assertEqual((v['broadcasts'][0]['peak_source'],v['broadcasts'][0]['peak_viewers']), ('summary',8))
        # An unconfirmed local stop cannot manufacture a second broadcast.
        self.broadcast(start+7200, confirmed=False)
        self.assertEqual(self.value()['totals']['broadcasts'], 1)

    def test_provider_end_persists_and_youtube_needs_positive_confirmation(self):
        start = time.time()-300
        sid = self.broadcast(start,end=False)
        with self.store.connect() as db:
            db.execute('INSERT INTO studio_audience VALUES(?,?,?,?,?,?)',(sid,start+120,'offline',None,None,None))
            db.execute('INSERT INTO studio_audience VALUES(?,?,?,?,?,?)',(sid,start+180,'unavailable',None,None,None))
            row, = project(db, start+200)['broadcasts']
            self.assertEqual((row['state'],row['state_source']), ('ended','helix'))
        second = self.broadcast(start+200,platform='youtube',end=False,confirmed=False)
        with self.store.connect() as db:
            self.assertEqual(project(db,start+210)['broadcasts'][0]['state'], 'unknown')
            db.execute('INSERT INTO studio_provider_events VALUES(?,?,?)',(second,start+205,'live'))
            row = project(db,start+210)['broadcasts'][0]
            self.assertEqual((row['state'],row['state_source']), ('live','youtube'))
            db.execute('INSERT INTO studio_provider_events VALUES(?,?,?)',(second,start+240,'ended'))
            self.assertEqual(project(db,start+250)['broadcasts'][0]['state'],'ended')

    def test_coverage_gaps_and_historical_schema(self):
        start = time.time()-120
        inbox = CommentInbox(self.store, scope='cloud')
        obs = StudioObservations(inbox,'twitch',False,'twitch','gap',sample_rate=48000,fps=30)
        for stamp in (0,1,2,12,13):
            obs.chat_sample(True, start+stamp)
        obs.chat_sample(False, start+14)
        for stamp in (19,20):
            obs.chat_sample(True, start+stamp)
        obs.close(30*60,'stopped')
        with self.store.connect() as db:
            db.execute('UPDATE studio_sessions SET started=?,stopped=?',(start,start+60))
            self.assertEqual(project(db,start+70)['broadcasts'][0]['chat']['observed_seconds'],4)
            db.execute('PRAGMA user_version=20')
            old = project(db,start+70)['broadcasts'][0]
            self.assertEqual(old['chat']['status'],'unknown')
            self.assertIsNone(old['speakers'])
        with closing(sqlite3.connect(':memory:')) as old:
            old.execute('PRAGMA user_version=19')
            self.assertFalse(project(old,start)['pipeline']['observations_ready'])

    def test_window_and_all_time_viewers_independent_of_log_limit(self):
        start = datetime(2026,11,24,11,tzinfo=JST)
        with self.store.connect() as db:
            for week in range(-1,60):
                t = (start+timedelta(weeks=week)).timestamp()
                import_observation(db, dict(kind='summary',platform='twitch',stream_id=f'w{week}',started_at=t,
                                            ended_at=t+3600,unique_viewers=10))
            v = project(db, time.time())
        self.assertEqual((len(v['broadcasts']),v['totals']['broadcasts'],v['totals']['unique_viewers']), (60,61,610))
        self.assertEqual(v['totals']['viewer_summaries'],61)
        slot = next(s for s in v['slots'] if s['id']=='en-11')
        self.assertEqual((slot['broadcasts'],slot['eligible'],slot['unique_viewers']), (0,0,None))

    def test_cohort_uses_first_observed_broadcast_and_own_reply(self):
        start = datetime(2026,11,29,20,tzinfo=JST).timestamp()
        self.broadcast(start, [('a',1),('b',2),('b',5),('a',6),('c',8)])
        second = self.broadcast(start+86400, [('a',1),('new',2),('new',5)], end=False)
        with self.store.connect() as db:
            # b wrote twice without a completed reply, c's reply has no follow-up.
            db.execute("UPDATE studio_inputs SET played_at=NULL WHERE received IN (?,?)", (start+2,start+5))
            v = project(db,start+86420)
            self.assertEqual(v['totals']['cohort'], {'first_seen':3,'replied':2,'spoke_again':1})
            self.assertEqual(v['totals']['repeat_viewers']['on_two_or_more_days'],0)
            db.execute('UPDATE studio_sessions SET stopped=? WHERE session=?',(start+86460,second))
            v = project(db,start+86500)
        self.assertEqual(v['totals']['cohort'], {'first_seen':4,'replied':3,'spoke_again':2})
        self.assertEqual(v['totals']['repeat_viewers']['on_two_or_more_days'],1)

    def test_live_slot_shows_current_cohort_cost_and_duration_without_entering_final_rate(self):
        start = datetime(2026,11,24,17,tzinfo=JST).timestamp()
        session = self.broadcast(start,[('new',1),('new',8),('new',15)],end=False)
        self.bind(session,start)
        with self.store.connect() as db:
            db.execute('UPDATE studio_sessions SET frames=NULL WHERE session=?',(session,))
            db.execute('INSERT INTO studio_audience(session,observed,status,stream_id,viewers,stream_started) VALUES(?,?,?,?,?,?)',
                       (session,start+60,'live',f'stream{int(start)}',0,start))
            v = project(db, start+62)
            s = next(slot for slot in v['slots'] if slot['id']=='ja-17')
            self.assertEqual((s['eligible'],s['broadcasts'],s['activity']['live'],s['activity']['comments']), (0,0,1,3))
            self.assertEqual(s['activity']['cohort'],dict(first_seen=1,replied=1,spoke_again=1))
            self.assertEqual((v['observed']['cohort'],v['totals']['cohort']),(s['activity']['cohort'],None))
            row, = v['broadcasts']
            self.assertEqual((row['latest_viewers'],row['minutes'],row['minutes_source']),(0,1.0,'last_live'))
            v = project(db, start+215)
            self.assertEqual((v['observed']['live'],v['observed']['last_seen'],v['observed']['comments']),(0,1,3))
            self.assertEqual(v['broadcasts'][0]['minutes'],1.0)
            db.execute('UPDATE studio_sessions SET stopped=? WHERE session=?',(start+220,session))
            v = project(db, start+230)
            self.assertEqual(v['totals']['cohort'],v['observed']['cohort'])
            self.assertEqual(v['observed']['comments'],3)

    def test_slot_percentile_pools_individual_samples_across_broadcasts(self):
        first = datetime(2026,11,24,17,tzinfo=JST).timestamp()
        for j in range(2):
            start = first+j*86400
            session = self.broadcast(start,[('x',i) for i in range(11)])
            self.bind(session,start)
            with self.store.connect() as db:
                inputs = list(db.execute('SELECT receipt,turn_id FROM studio_inputs WHERE session=? ORDER BY received',(session,)))
                sample = 0
                for i,(receipt,_) in enumerate(inputs):
                    sample += 480 + (43200 if j and i>8 else 4800)
                    db.execute("""UPDATE studio_inputs SET previous_turn=?,first_sample=?,end_sample=?,first_written=?,last_written=?
                        WHERE receipt=?""",(inputs[i-1][1] if i else None,sample,sample+480,start+35+i,start+45+i,receipt))
        v = self.value(first+86500)
        gaps = [b['gap'] for b in v['broadcasts']]
        self.assertEqual([g['n'] for g in gaps],[10,10])
        self.assertEqual([g['p95_ms'] for g in gaps],[None,None])
        self.assertEqual(sorted(g['sample_p95_ms'] for g in gaps),[100,900])
        active = next(s['activity'] for s in v['slots'] if s['id']=='ja-17')
        self.assertEqual(active['gap'],v['observed']['gap'])
        self.assertEqual((active['gap']['n'],active['gap']['p50_ms'],active['gap']['p95_ms']),(20,100,900))

    def test_gap_threshold_api_attempts_and_discarded_prefetch(self):
        start = time.time()-200
        sid = self.broadcast(start, [('speaker',n) for n in range(22)])
        with self.store.connect() as db:
            db.execute('UPDATE studio_sessions SET prefetch=1 WHERE session=?',(sid,))
            inputs = list(db.execute('SELECT receipt,turn_id FROM studio_inputs WHERE session=? ORDER BY received',(sid,)))
            for n,(receipt,turn) in enumerate(inputs):
                db.execute("""UPDATE studio_inputs SET previous_turn=?,first_sample=?,end_sample=?,first_written=?,last_written=?
                              WHERE receipt=?""",(inputs[n-1][1] if n else None,n*9600,n*9600+4800,start+n+2,start+n+3,receipt))
            turn = inputs[-1][1]
            db.execute("UPDATE studio_inputs SET prefetched=1,played_at=NULL,status='cancelled',first_sample=NULL WHERE turn_id=?", (turn,))
        for _ in range(2):
            key = self.store.prepare_call(turn,'tts','synthesize',{'chunk':0,'characters':4},model='en-US-Wavenet-D',mode='cloud')
            self.store.call_state(key,'failed',attempted=True)
        r = self.value()['broadcasts'][0]
        self.assertEqual(r['prefetch'],'prefetch')
        self.assertEqual((r['gap']['n'],r['gap']['p95_ms'],r['gap']['missing'],r['unplayed']), (20,100,1,1))
        api = r['api']['by_provider']['tts']
        self.assertEqual((api['attempts'],api['failed'],api['retries'],api['discarded_prefetch_attempts']), (2,2,1,2))
        with self.store.connect() as db:
            db.execute('UPDATE studio_inputs SET first_sample=NULL WHERE receipt=?', (inputs[-2][0],))
            gap = project(db,time.time())['broadcasts'][0]['gap']
        self.assertEqual((gap['n'],gap['p95_ms']), (19,None))


if __name__ == '__main__':
    unittest.main()
