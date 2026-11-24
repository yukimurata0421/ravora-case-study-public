from datetime import date, datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from ravora import twitch_schedule_cli as command
from ravora.database import Database
from ravora.twitch_schedule import JST, DURATION, next_slot, slot_at, slots_on, sample_slots
from ravora.twitch_identity import TwitchError
try:
    from ravora.twitch_market import market, tagged
    from ravora.twitch_schedule_api import offline
except ModuleNotFoundError as exc:
    if exc.name not in ('requests', 'websocket'):
        raise
    market = None
try:
    from ravora.live_program import LiveProgram
except ModuleNotFoundError as exc:
    if exc.name not in ('av', 'numpy', 'PIL'):
        raise
    LiveProgram = None


class ScheduleTests(unittest.TestCase):
    def test_unconfigured_account_blocks_live_commands_before_accessing_a_provider(self):
        mask = os.umask(0o027)
        self.addCleanup(os.umask, mask)
        with patch.dict('os.environ', RAVORA_TWITCH_CHANNEL=''), patch.object(command, 'collect') as sample:
            with self.assertRaisesRegex(TwitchError, 'TWITCH_CHANNEL_LOGIN_REQUIRED'):
                command.main(['sample'])
            sample.assert_not_called()
        self.assertEqual(os.umask(0o027), 0o027)

    def test_calendar_and_adjacent_windows(self):
        day=date(2026,11,23)
        days=[slots_on(day+timedelta(days=i)) for i in range(7)]
        self.assertEqual([len(d) for d in days],[6,7,6,7,6,7,7])
        self.assertEqual(sum(len(d) for d in days)*2,92)
        for daily in days:
            for slot in daily:
                self.assertEqual(slot_at(slot.start,launching=True),slot)
                self.assertIsNone(slot_at(slot.start+240,launching=True))
                self.assertEqual(slot.end-slot.start,7200)
        second=days[0][1]
        self.assertEqual(slot_at(second.start),second)
        self.assertEqual(sample_slots(second.start),list(days[0][:2]))
        self.assertEqual(next_slot(days[-1][-1].end).arm,'en-03')

    def test_claim_is_durable_and_failure_is_not_retried(self):
        slot=slots_on(date(2026,11,24))[0]
        with tempfile.TemporaryDirectory() as folder:
            db=Database(Path(folder)/'db')
            studio=Mock(side_effect=SystemExit('TWITCH_CHANNEL_ALREADY_LIVE'))
            with self.assertRaises(SystemExit):
                command.launch(slot,db,studio,clock=lambda:slot.start+2)
            self.assertEqual(command.launch(slot,db,studio,clock=lambda:slot.start+3),'already_claimed')
            self.assertEqual(studio.call_count,1)
            with db.connect() as conn:
                self.assertEqual(conn.execute('SELECT result,reason FROM twitch_schedule_runs').fetchone(),
                                 ('blocked','TWITCH_CHANNEL_ALREADY_LIVE'))

    @unittest.skipUnless(market, 'youtube-live extra required')
    def test_waits_only_for_own_previous_stream(self):
        with patch('ravora.twitch_schedule_api.query',side_effect=[{'data':[{'id':'old','user_id':'ray'}]},{'data':[]},{'data':[]}]), \
                patch('ravora.twitch_schedule_api.time.sleep') as sleep:
            offline(None,None,'ray','old',float('inf'))
            self.assertEqual(sleep.call_count,2)
        with patch('ravora.twitch_schedule_api.query',return_value={'data':[{'id':'other','user_id':'ray'}]}):
            with self.assertRaises(TwitchError):
                offline(None,None,'ray','old',float('inf'))

    def test_elapsed_run_without_helix_is_unconfirmed(self):
        slot = slots_on(date(2026, 11, 24))[0]
        now = [slot.start]
        def studio(args, *, scheduled):
            self.assertEqual(scheduled, slot)
            self.assertNotIn('--dell-mic', args)
            now[0] = slot.end
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder)/'db')
            self.assertEqual(command.launch(slot, db, studio, clock=lambda:now[0]), 'unconfirmed')

    @unittest.skipUnless(market, 'youtube-live extra required')
    def test_shared_boundary_collects_the_market_and_peers_once(self):
        slot = slots_on(date(2026, 11, 24))[1]
        window = sample_slots(slot.start+1)
        with tempfile.TemporaryDirectory() as folder, patch('ravora.twitch_market.sample',
                return_value=(('complete',1,4,8,8),(slot.start+2,'ray',None))) as fetch:
            path = Path(folder)/'db'
            self.assertEqual(command.collect(window, path, slot.start+1),'complete')
            self.assertEqual(command.collect(window, path, slot.start+1),'already_sampled')
            fetch.assert_called_once()
            with Database.existing(path).connect() as db:
                rows = db.execute('SELECT tick,peers_at FROM twitch_schedule_market ORDER BY scheduled_start').fetchall()
            self.assertEqual(rows, [(24, slot.start+2), (0, slot.start+2)])

    @unittest.skipUnless(LiveProgram, 'youtube-live extra required')
    def test_scheduled_initiative_uses_its_language(self):
        program=LiveProgram.__new__(LiveProgram)
        program.initiative_language='ja'
        program.reply_profiles={'ja':('ja_settings','ja_text','ja_voice'),'en':('en_settings','en_text','en_voice')}
        bridge=Mock()
        program._profile(bridge,{'source':'ray-initiative'},'en')
        self.assertEqual(bridge.controller.speech,'ja_voice')
        program._profile(bridge,{'source':'twitch','comment':'How are you?'},'ja')
        self.assertEqual(bridge.controller.speech,'en_voice')

    @unittest.skipUnless(market, 'youtube-live extra required')
    def test_paging_deduplicates_and_truncation_is_explicit(self):
        def row(id,views,tag):
            return {'user_id':id,'user_login':id,'type':'live','game_id':'c','language':'en','tags':[tag],'viewer_count':views}
        self.enterContext(patch.dict('os.environ', RAVORA_TWITCH_CHANNEL='fixture_broadcaster'))
        pages=[{'data':[row('a',10,'VTuber'),row('b',100,'other')],'pagination':{'cursor':'next'}},
               {'data':[row('a',11,'VTuber'),row('c',5,'EN_VTuber')],'pagination':{}}]
        with patch('ravora.twitch_market.query',side_effect=pages):
            self.assertEqual(market(None,None,'c','en'),('complete',2,2,15,15))
        with patch('ravora.twitch_market.query',return_value=pages[0]):
            self.assertEqual(market(None,None,'c','en',max_pages=1)[0],'partial')
        self.assertTrue(tagged(['新人ＶＴｕｂｅｒ']))


if __name__=='__main__':
    unittest.main()
