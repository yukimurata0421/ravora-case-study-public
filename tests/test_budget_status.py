from importlib.util import find_spec
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ravora.budget_status import (SCHEDULED_REPLY_USD, SCHEDULED_STT_SECONDS, SCHEDULED_TTS_CHARS, budget_status,
                                  scheduled_limits)
from ravora.conversation import MemoryStore
from ravora.providers import ProviderError
from ravora.ray_public import dumps, project
from ravora.scheduled_budget import JEV_RESERVATION

CONFIG = SimpleNamespace(monthly_reply_llm_usd=0, monthly_tts_chars=1_800_000, stt_monthly_audio_seconds=3600)


class BudgetStatusTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.store = MemoryStore(Path(folder)/'test.db')
        self.store.scheduled_budget = True
        self.store.start('turn', 'cloud:fixture', 'synthetic comment')

    def status(self, now=None):
        with self.store.connect() as db:
            return budget_status(db, scheduled_limits(CONFIG), now or time.time())

    def test_limits_follow_the_scheduled_launcher(self):
        high = SimpleNamespace(monthly_reply_llm_usd=100, monthly_tts_chars=2_000_000,
                               stt_monthly_audio_seconds=40_000)
        limits = scheduled_limits(high)
        self.assertEqual((limits['reply_llm_usd'], limits['tts_characters'], limits['cloud_stt_seconds']),
                         (SCHEDULED_REPLY_USD, SCHEDULED_TTS_CHARS, SCHEDULED_STT_SECONDS))
        self.assertEqual(scheduled_limits(CONFIG), dict(reply_llm_usd=20, tts_characters=900_000, auxiliary_usd=2.0, cloud_stt_seconds=3600))

    def test_use_agrees_with_the_checks_that_stop_dispatch(self):
        self.store.reserve(1200, 900_000)
        reply = self.store.prepare_call('turn', 'gemini', 'stream_generate', {}, model='gemini-3.5-flash-lite', mode='cloud')
        self.store.dispatch_budgeted_reply(reply, 1000, 400, 1, 20)
        self.store.dispatch_scheduled_reaction(self.store.prepare_call('turn', 'jev', 'reaction', {}, model='jev-1.14.0', mode='cloud'))
        held = self.store.prepare_call('turn', 'jev', 'reaction', {}, model='jev-1.14.0', mode='cloud')  # never sent
        s = self.status()
        self.assertEqual(s['tts_characters']['used'], 1200)
        self.assertEqual(s['reply_llm_usd']['used'], s['reply_llm_usd']['last_24h'])
        self.assertAlmostEqual(s['auxiliary_usd']['used'], JEV_RESERVATION/1e6)
        # One more microdollar or character than the reported remainder is refused; the remainder itself fits.
        with self.assertRaisesRegex(ProviderError, 'TTS'):
            self.store.reserve(900_000-1200+1, 900_000)
        self.store.reserve(900_000-1200, 900_000)
        self.assertEqual(self.status()['tts_characters']['used'], 900_000)
        with patch('ravora.scheduled_budget.AUX_LIMIT', round(s['auxiliary_usd']['used']*1e6)+JEV_RESERVATION-1):
            with self.assertRaisesRegex(ProviderError, 'AUX'):
                self.store.dispatch_scheduled_reaction(held)
        with patch('ravora.scheduled_budget.AUX_LIMIT', round(s['auxiliary_usd']['used']*1e6)+JEV_RESERVATION):
            self.store.dispatch_scheduled_reaction(held)

    def turn(self, name, source=None, session=None):
        self.store.start(name, 'cloud:fixture', 'synthetic comment')
        with self.store.connect() as db:
            if source:
                db.execute("INSERT INTO source_events(id,platform,external_event_id,stream_id,viewer_id,viewer_key,text,received_at,expires_at) "
                           "VALUES(?,?,?,'s','v','k','x',?,?)", (name, source, name, time.time(), time.time()+60))
                db.execute('UPDATE conversation_turns SET source_event_id=?,usage_session=? WHERE turn_id=?', (name, session, name))

    def test_splits_follow_broadcast_and_source_and_add_up(self):
        with self.store.connect() as db:
            for session, platform in (('live', 'twitch'), ('rehearsal', 'local')):
                db.execute("INSERT INTO studio_sessions(session,platform,mode,sample_rate,fps,prefetch,chat_source,timing_write_losses,"
                           "started,identity_conflict,chat_tracked) VALUES(?,?,'cloud',48000,30,0,'twitch',0,?,0,0)", (session, platform, time.time()))
        self.turn('comment', 'twitch', 'live')
        self.turn('alone', 'ray-initiative', 'live')
        self.turn('practice', 'twitch', 'rehearsal')
        for turn, voice, size in (('comment', 'ja-JP-X', 30), ('alone', 'en-US-X', 50), ('practice', 'en-US-X', 7)):
            self.store.reserve(size, 900_000)
            call = self.store.prepare_call(turn, 'tts', 'synthesize', {'characters': size}, model=voice, mode='cloud')
            self.store.call_state(call, 'accepted', attempted=True)
        self.store.reserve(1000, 900_000)
        for turn in ('comment', 'alone'):
            call = self.store.prepare_call(turn, 'gemini', 'generate', {}, model='gemini-3.5-flash-lite', mode='cloud')
            self.store.dispatch_budgeted_reply(call, 1000, 400, 1, 20)
        s = self.status()
        used = lambda key, split: {p['id']: p['used'] for p in s[key][split]}
        self.assertEqual(used('tts_characters', 'purposes'), dict(viewer=30, unprompted=50, outside=7, not_itemized=1000))
        self.assertEqual(used('tts_characters', 'services'), dict(ja=30, en=57, not_itemized=1000))
        self.assertEqual(set(used('reply_llm_usd', 'purposes')), {'viewer', 'unprompted'})
        self.assertEqual(s['tts_characters']['last_24h'], 87)
        for key in ('reply_llm_usd', 'tts_characters', 'auxiliary_usd', 'cloud_stt_seconds'):
            for split in ('purposes', 'services'):
                self.assertAlmostEqual(sum(p['used'] for p in s[key][split]), s[key]['used'], places=5)

    def test_month_rollover_and_unknown_rate(self):
        self.store.reserve(50, 900_000)
        call = self.store.prepare_call('turn', 'tts', 'synthesize', {}, model='voice', mode='cloud')
        self.store.call_state(call, 'accepted', attempted=True)
        s = self.status()
        self.assertIsNone(s['tts_characters']['last_24h'])
        later = self.status(time.time()+40*86400)
        self.assertEqual((later['tts_characters']['used'], later['tts_characters']['last_24h']), (0, 0))
        self.assertNotEqual(later['month'], s['month'])
        december = self.status(1796050800.0+9*3600)
        self.assertTrue(december['resets_jst'].endswith('-01T00:00+09:00'))

    @unittest.skipUnless(find_spec('jsonschema'), 'observations extra required')
    def test_projection_matches_the_public_contract(self):
        from jsonschema import Draft7Validator
        from ravora.ray_public_contract import SOURCE_SCHEMA
        with self.store.connect() as db:
            full, bare = project(db, time.time(), scheduled_limits(CONFIG)), project(db, time.time())
        for value in (full, bare):
            value['pipeline'].pop('collected_at', None)
            Draft7Validator(SOURCE_SCHEMA).validate(value)
        self.assertIsNone(bare['budgets'])
        self.assertNotIn('fixture', dumps(full))


if __name__ == '__main__':
    unittest.main()
