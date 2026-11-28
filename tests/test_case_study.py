import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ravora.case_study import early_reaction_replay,offline_settings,replay,relationship_replay
from ravora.context_prompt import context_text
from ravora.database import MIGRATIONS
from ravora.edge_grpc import EdgeClients


class CaseStudyTests(unittest.TestCase):
    def test_replay_closes_its_temporary_state_and_stays_offline(self):
        with tempfile.TemporaryDirectory() as parent,patch('tempfile.tempdir',parent), \
                patch.object(EdgeClients,'open',side_effect=AssertionError('LIVE_GRPC_FORBIDDEN')):
            result=asyncio.run(replay())
            self.assertEqual(result['status'],'PASS_CASE_STUDY_REPLAY')
            self.assertEqual(result['external_api_calls'],0)
            self.assertEqual(set(result['tier_order']),{1,2,3})
            self.assertTrue(result['pcm_before_text_complete'])
            self.assertTrue(result['stale_audio_fenced_after_cancel'])
            self.assertTrue(result['reviewed_summary_shared_without_private_transcript'])
            self.assertEqual(list(Path(parent).iterdir()),[])
    def test_inherited_credentials_do_not_enable_the_public_demo(self):
        with patch.dict('os.environ',{'RAVORA_API_ENABLED':'true','RAVORA_STT_API_ENABLED':'true',
                'RAVORA_MEMORY_API_ENABLED':'true','GEMINI_API_KEY':'not-a-real-key'}):
            settings=offline_settings()
        self.assertFalse(settings.api_enabled);self.assertFalse(settings.stt_api_enabled)
        self.assertFalse(settings.memory_api_enabled);self.assertEqual(settings.gemini_key,'')
    def test_schema_resources_are_inside_the_package(self):
        self.assertTrue(MIGRATIONS.is_dir());self.assertEqual(len(list(MIGRATIONS.glob('*.sql'))),32)
        self.assertIn('ravora',MIGRATIONS.parts)

    def test_relationship_replay_is_offline_and_cleans_synthetic_state(self):
        with tempfile.TemporaryDirectory() as parent,patch('tempfile.tempdir',parent), \
                patch.object(EdgeClients,'open',side_effect=AssertionError('LIVE_GRPC_FORBIDDEN')):
            result=relationship_replay()
            self.assertEqual(result['status'],'PASS_RELATIONSHIP_REPLAY')
            self.assertEqual(result['external_api_calls'],0)
            self.assertEqual(result['same_post_count']['twenty_day_units'],60)
            self.assertFalse(result['generated_response_quality_evaluated'])
            self.assertEqual(list(Path(parent).iterdir()),[])

    def test_early_reaction_replay_preserves_final_only_generation_and_two_stages(self):
        with tempfile.TemporaryDirectory() as parent,patch('tempfile.tempdir',parent), \
                patch.object(EdgeClients,'open',side_effect=AssertionError('LIVE_GRPC_FORBIDDEN')):
            result=asyncio.run(early_reaction_replay())
            self.assertTrue(result['reaction_before_input_end'])
            self.assertTrue(result['llm_received_final_only'])
            self.assertEqual(result['reaction_slots_used'],2)
            self.assertEqual([e['transition'] for e in result['reactions']],['switch','return_neutral'])
            self.assertEqual(result['external_api_calls'],0)
            self.assertFalse(result['latency_measurement'])
            self.assertEqual(list(Path(parent).iterdir()),[])


if __name__=='__main__':unittest.main()
