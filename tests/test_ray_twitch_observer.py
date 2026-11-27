from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

spec = spec_from_file_location('ray_observer',Path(__file__).resolve().parents[1]/'deploy/k3s/ray-monitor/observer.py')
observer = module_from_spec(spec)
spec.loader.exec_module(observer)


class MonitoringTests(unittest.TestCase):
    def test_provider_requires_successful_metadata_and_explicit_null_stream(self):
        api = Mock()
        gql = api.get_info_extractor.return_value._download_gql
        gql.return_value = [{'data':{'user':{'stream':None}}}]
        self.assertIsNone(observer.provider(api))
        gql.return_value = [{'data':{'user':{'stream':{'id':'123','type':'live'}}}}]
        self.assertEqual(observer.provider(api),'123')
        gql.return_value = [{'errors':['failure'],'data':{'user':{'stream':None}}}]
        with self.assertRaises(ValueError): observer.provider(api)

    def test_uses_last_complete_segment_and_checks_both_clocks_and_replay(self):
        playlist = '#EXTM3U\n#EXTINF:2,live\ns1\n#EXTINF:2,live\ns2\n#EXT-X-TWITCH-PREFETCH:s3'
        self.assertEqual(observer.last_segment(playlist),'s2')
        api = Mock()
        api.extract_info.return_value = dict(uploader_id=observer.CHANNEL,id='123',is_live=True,
            formats=[dict(height=720,protocol='m3u8',url='https://example.test/live')])
        frames = [dict(media_type=k,best_effort_timestamp_time=str(t)) for k in ('audio','video') for t in (1,2)]
        response = Mock(returncode=0)
        def get(url,_):
            return (playlist.encode() if url.endswith('live') else b'segment'),url
        with patch.object(observer,'fetch',side_effect=get), patch.object(subprocess,'run',return_value=response):
            response.stdout=json.dumps({'frames':frames})
            first=observer.media(api,'123',None)
            self.assertEqual(first['media'],'advancing')
            self.assertEqual(observer.media(api,'123',first['segment_hash'])['reason'],'same_segment')
            response.stdout=json.dumps({'frames':frames[2:]})
            self.assertEqual(observer.media(api,'123',None)['reason'],'audio_time')
        api.extract_info.return_value['id']='456'
        self.assertEqual(observer.media(api,'123',None)['media'],'unknown')

    def test_media_timeout_preserves_provider_and_total_outage_stays_unknown(self):
        partial=b'{"provider":"live","media":"unknown","reason":"media_timeout"}\n'
        with patch.object(subprocess,'run',side_effect=subprocess.TimeoutExpired('sample',24,output=partial)):
            self.assertEqual(observer.bounded_sample('123','',True)['provider'],'live')
        with patch.object(subprocess,'run',side_effect=subprocess.TimeoutExpired('sample',24)):
            self.assertEqual(observer.bounded_sample('123','',True)['provider'],'unknown')
