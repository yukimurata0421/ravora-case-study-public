from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ravora.conversation import MemoryStore
from ravora.providers import ProviderError
from ravora.scheduled_budget import JEV_RESERVATION, MEMORY_RESERVATION, reserve_auxiliary
from ravora.studio_report import estimate


class ScheduledBudgetTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.store = MemoryStore(Path(folder)/'test.db')
        self.store.scheduled_budget = True
        self.store.start('turn','cloud:fixture','synthetic comment')

    def reaction(self, model='jev-1.14.0'):
        return self.store.prepare_call('turn','jev','reaction',{},model=model,mode='cloud')

    def test_concurrent_dispatches_share_a_month_and_failed_calls_keep_reservations(self):
        keys = [self.reaction(),self.reaction()]
        def send(key):
            try:
                self.store.dispatch_scheduled_reaction(key)
                return True
            except ProviderError as exc:
                self.assertEqual(str(exc),'LOCAL_MONTHLY_AUX_LIMIT')
                return False
        with patch('ravora.scheduled_budget.AUX_LIMIT',JEV_RESERVATION):
            with ThreadPoolExecutor(2) as pool:
                self.assertEqual(sorted(pool.map(send,keys)),[False,True])
            with self.store.connect() as db:
                key=db.execute('SELECT id FROM provider_calls WHERE attempted=1').fetchone()[0]
            self.store.call_state(key,'outcome_unknown')
            self.assertFalse(send(self.reaction()))
            with self.assertRaisesRegex(ProviderError,'UNPRICED'):
                self.store.dispatch_scheduled_reaction(self.reaction('unknown-model'))

    def test_legacy_usage_counts_and_a_memory_reservation_shares_the_pool(self):
        import time
        old=self.reaction()
        self.store.call_state(old,'accepted',attempted=True)
        self.store.call_usage(old,{'input_tokens':1000})
        key=self.store.prepare_call('turn','gemini','extract_memory',{},model='gemini-3.5-flash-lite',mode='cloud')
        with patch('ravora.scheduled_budget.AUX_LIMIT',MEMORY_RESERVATION+41):
            with self.store.connect() as db, self.assertRaisesRegex(ProviderError,'AUX_LIMIT'):
                db.execute('BEGIN IMMEDIATE')
                reserve_auxiliary(db,key,time.time())
        self.assertAlmostEqual(estimate(dict(cost_estimate=None,model='jev-1.14.0',provider='jev',input_tokens=1000)),.000042)
