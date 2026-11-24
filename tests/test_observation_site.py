import contextlib
import io
from importlib.util import find_spec
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ravora import observation_site as collector
from ravora.ray_public_contract import validate
from ravora.ray_page import render


def sample(now):
    value = json.loads((Path(__file__).parent/'fixtures/ray-observation-v4.json').read_text())
    value['generated_at'] = now
    return value


class ValidateTests(unittest.TestCase):
    @unittest.skipUnless(find_spec('jsonschema'), 'observations extra required')
    def test_v5_counts_and_ended_state_reach_the_card(self):
        now = time.time()
        v = sample(now)
        v['schema'] = 'ravora.ray_public_observation.v5'
        for b in v['broadcasts']:
            b.update(state='ended',state_source='studio_stop',speech_policy='unrecorded',audience_health=None,
                     response_sli=dict(eligible=4,started=4,within_3s=2,within_5s=3,budget_blocked=0))
        validate(v,now)
        v['pipeline']['collected_at']=now
        page=render(v)
        self.assertIn('Latest broadcast finished',page)
        self.assertIn('Studio output stopped',page)
        self.assertIn('3/4',page)
        self.assertNotIn('pill wait">Unconfirmed',page)
        for key in v['broadcasts'][0]['response_sli']:
            v['broadcasts'][0]['response_sli'][key]=0
        self.assertIn('No eligible comments',render(v))
        zero = v['broadcasts'][0]['response_sli']
        v['broadcasts'][0]['response_sli']=None
        self.assertIn('Eligible comments unobserved',render(v))
        v['broadcasts'][0]['response_sli']=zero
        del v['pipeline']['collected_at']
        v['broadcasts'][0]['response_sli']['within_5s']=1
        with self.assertRaises(ValueError):
            validate(v,now)

    @unittest.skipUnless(find_spec('jsonschema'), 'observations extra required')
    def test_exporter_command_renders_the_fictional_broadcast(self):
        example = Path(__file__).resolve().parents[1] / 'examples/broadcast_observations.py'
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collector.main(['--output', temp, '--source-command', sys.executable, str(example)]), 0)
            value = json.loads((Path(temp) / 'ray-observation.json').read_text())
            self.assertEqual(value['observed']['cohort'], {'first_seen': 1, 'replied': 1, 'spoke_again': 1})
            self.assertEqual(value['observed']['gap']['sample_p95_ms'], 250)
            self.assertIsNone(value['observed']['gap']['p95_ms'])
            broadcast = value['broadcasts'][0]
            self.assertEqual(broadcast['speech_policy'], 'short-turn-v1')
            self.assertEqual(broadcast['response_sli'], {
                'eligible': 4, 'started': 3, 'within_3s': 3, 'within_5s': 3, 'budget_blocked': 1})
            page = (Path(temp) / 'ray/index.html').read_text()
            self.assertIn('Within 5 seconds', page)
            self.assertIn('3/4', page)


    @unittest.skipUnless(find_spec('jsonschema'), 'observations extra required')
    def test_accepts_studio_export_and_full_log(self):
        now = time.time()
        value = sample(now)
        validate(value, now)
        value['broadcasts'] *= 60
        validate(value, now)
        self.assertLess(len(json.dumps(value).encode()), collector.MAX_BYTES)

    @unittest.skipUnless(find_spec('jsonschema'), 'observations extra required')
    def test_rejects_private_fields_invalid_data_and_stale_exports(self):
        now = time.time()
        for mutate in (lambda v: v['broadcasts'][0].update(viewer='alice'),
                       lambda v: v['totals'].update(session='x'),
                       lambda v: v['broadcasts'][0]['api']['by_provider'].update(private_name={}),
                       lambda v: v['slots'][0].update(audience='private name'),
                       lambda v: v['broadcasts'][0].update(started_jst='2026-11-29 20:01'),
                       lambda v: v.update(generated_at=now-3600),
                       lambda v: v['broadcasts'][0].update(speakers=-1),
                       lambda v: v['broadcasts'][0].update(cost_usd=float('nan')),
                       lambda v: v['pipeline'].update(collected_at=now),
                       lambda v: v['slots'].reverse(),
                       lambda v: v['slots'][0].update(days='tue-thu-sat-sun'),
                       lambda v: v['broadcasts'][0]['gap'].update(p95_ms=123),
                       lambda v: v['observed']['gap'].update(sample_p95_ms=None),
                       lambda v: v['slots'][4]['activity']['cohort'].update(account_id='x')):
            v = sample(now)
            mutate(v)
            with self.assertRaises(ValueError):
                validate(v, now)

    def test_exporter_must_finish_successfully_within_the_size_limit(self):
        with self.assertRaises(subprocess.CalledProcessError):
            collector.source_output([sys.executable, '-c', 'print("{}"); raise SystemExit(2)'])
        with self.assertRaisesRegex(ValueError, 'SOURCE_TOO_LARGE'):
            collector.source_output([sys.executable, '-c', f'print("a" * {collector.MAX_BYTES})'])

    @unittest.skipUnless(find_spec('jsonschema'), 'observations extra required')
    def test_collection_timestamp_only_advances_after_valid_fetch(self):
        now = time.time()
        good = json.dumps(sample(now)).encode()
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp)/'ray-observation.json'
            args = ['--output', temp, '--source-command', 'example-exporter']
            with patch.object(collector,'source_output',return_value=good) as run, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(collector.main(args), 0)
                for asset in collector.ASSETS:
                    self.assertEqual((dest.parent/'assets'/asset).read_bytes(), (collector.TEMPLATE.parent/asset).read_bytes())
                page = dest.parent/'ray/index.html'
                initial_html = page.read_bytes()
                self.assertIn(b'17:00',initial_html)
                last = dest.read_bytes()
                self.assertGreaterEqual(json.loads(last)['pipeline']['collected_at'], now)
                run.return_value = b'{"private":"value"}'
                self.assertNotEqual(collector.main(args), 0)
                self.assertEqual(dest.read_bytes(), last)
                self.assertEqual(page.read_bytes(),initial_html)
                run.side_effect = subprocess.TimeoutExpired('ssh', 30)
                self.assertNotEqual(collector.main(args), 0)
                self.assertEqual(dest.read_bytes(), last)
                self.assertEqual(page.read_bytes(),initial_html)

    def test_initial_html_contains_live_gap_and_counts_without_javascript(self):
        now = time.time()
        v = sample(now)
        v['pipeline']['collected_at'] = now
        s = render(v)
        active = s.split('id="slot-ja-17"',1)[1].split('</tr>',1)[0]
        for label in ('17:00–19:00', '0 done · 1 live', 'Japan evening', '207 ms <span class="flag"', '1/20', '4 channels'):
            self.assertIn(label,active)
        cards = s.split('id="logBody"')[1]
        for label in ('pill live', 'Comment → on air', '207 ms', 'No API calls recorded', 'unique after summary'):
            self.assertIn(label,cards)
        self.assertIn('after summary import',s)
        self.assertIn('data-version="6"',s)
        self.assertIn('data-state="live"',s)
        self.assertIn('class="block ja has"',s)
        self.assertNotIn('Loading the latest',s)

    def test_rank_threshold_and_missing_reasons_in_html(self):
        from ravora.ray_page import percentile_text
        self.assertEqual(percentile_text(None),'No chat observation')
        self.assertIn('0/20',percentile_text(dict(n=0,p95_ms=None,sample_p95_ms=None)))
        self.assertIn('provisional (19/20)',percentile_text(dict(n=19,p95_ms=None,sample_p95_ms=164)))
        self.assertEqual(percentile_text(dict(n=20,p95_ms=164,sample_p95_ms=164)),'164.0 ms · n=20')

    def test_superseded_and_failed_page_writes_preserve_both_artifacts(self):
        now = time.time()
        newer = sample(now)
        older = sample(now-1)
        for value in (newer,older):
            value['pipeline']['collected_at']=now
        with tempfile.TemporaryDirectory() as temp:
            dest=Path(temp)/'ray-observation.json'
            page=Path(temp)/'ray/index.html'
            self.assertTrue(collector.write(dest.parent, newer))
            saved=(dest.read_bytes(),page.read_bytes())
            self.assertFalse(collector.write(dest.parent, older))
            self.assertEqual((dest.read_bytes(),page.read_bytes()),saved)
            newest=sample(now+1)
            newest['pipeline']['collected_at']=now+1
            replace=collector.os.replace
            def deny_page(src,dst):
                if dst==page: raise OSError('fixture')
                replace(src,dst)
            with patch.object(collector.os,'replace',side_effect=deny_page):
                with self.assertRaises(OSError): collector.write(dest.parent, newest)
            self.assertEqual((dest.read_bytes(),page.read_bytes()),saved)
            dest.write_text('invalid old data')
            self.assertTrue(collector.write(dest.parent, newest))
            self.assertEqual(json.loads(dest.read_bytes())['generated_at'],now+1)


if __name__ == '__main__':
    unittest.main()
