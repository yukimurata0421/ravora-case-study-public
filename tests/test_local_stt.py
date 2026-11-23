from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import wave

try:
    import numpy as np
except ModuleNotFoundError:
    raise unittest.SkipTest("requires numpy") from None

from ravora.airband_vad import analyze,frame_rms_db,VadConfig
from ravora.local_stt import LocalSTT,verify_model
from ravora.providers import ProviderError
from ravora.settings import ProviderSettings,SettingsError
from ravora.stt import AudioPacket


def speech_pcm():
    a=np.arange(11200,dtype=np.float32)/16000
    return np.concatenate([np.zeros(8000),.15*np.sin(a*2*np.pi*180),np.zeros(8000)]).astype(np.float32)


def packet(audio=None):
    if audio is None:audio=speech_pcm()
    buf=io.BytesIO()
    with wave.open(buf,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes((audio*32767).astype('<i2').tobytes())
    return AudioPacket.parse(buf.getvalue(),minimum_rms=0)


def slow_worker(pipe,_options):
    pipe.send({'ready':True,'identity':{'device':'cpu'}});pipe.recv();time.sleep(60)


class VadTests(unittest.TestCase):
    def test_original_airband_frame_equation_is_preserved(self):
        audio=speech_pcm();actual=frame_rms_db(audio)
        expected=np.array([20*np.log10(np.sqrt(np.mean(audio[i:i+480]**2)+1e-12)+1e-12) for i in range(0,len(audio)-479,240)],dtype=np.float32)
        np.testing.assert_array_equal(actual,expected)

    def test_silence_stationary_noise_tone_and_click_do_not_pass_coarse_gate(self):
        rng=np.random.default_rng(4)
        click=np.zeros(32000,dtype=np.float32);click[16000:16160]=.5
        for audio in (np.zeros(32000),rng.normal(0,.01,32000),np.ones(32000)*.1,click,np.sin(np.arange(32000)*2*np.pi*400/16000)*.2):
            with self.subTest(rms=float(np.sqrt(np.mean(audio**2)))):self.assertFalse(len(analyze(audio).audio))

    def test_merge_before_minimum_length_preserves_syllables(self):
        # Two voiced 120 ms pieces separated by 90 ms are a word, not two rejects.
        audio=np.zeros(32000,dtype=np.float32)
        for a,b in ((8000,9920),(11360,13280)):audio[a:b]=.1*np.sin(np.arange(b-a)*.2)
        value=analyze(audio);self.assertTrue(len(value.audio));self.assertEqual(len(value.mapping),1)
        self.assertLessEqual(value.mapping[0][0],8000);self.assertGreaterEqual(value.mapping[0][1],13280)

    def test_trimmed_audio_times_map_to_original_across_a_long_gap(self):
        voiced=speech_pcm()[8000:19200];audio=np.concatenate([np.zeros(8000),voiced,np.zeros(32000),voiced,np.zeros(8000)])
        value=analyze(audio);self.assertEqual(len(value.mapping),2)
        a,b,c,d=value.mapping[0];e,f,g,h=value.mapping[1]
        self.assertEqual(d,g);self.assertAlmostEqual(value.original_time(d/16000),e/16000)
        self.assertAlmostEqual(value.original_time(d/16000,end=True),b/16000)
        self.assertGreater(e-b,16000);self.assertLess(len(value.audio),len(audio))

    def test_noise_threshold_has_a_floor_and_is_adaptive(self):
        clean=analyze(speech_pcm());rng=np.random.default_rng(20)
        noisy=analyze(speech_pcm()+rng.normal(0,.008,len(speech_pcm())))
        self.assertEqual(clean.diagnostics['threshold_dbfs'],-50.)
        self.assertGreater(noisy.diagnostics['threshold_dbfs'],-40)
        self.assertTrue(len(noisy.audio))

    def test_bad_thresholds_and_pcm_are_rejected(self):
        for options in ({'margin_db':float('nan')},{'min_speech_ms':0},{'silence_ms':10},{'floor_db':0}):
            with self.assertRaises(ValueError):analyze(speech_pcm(),replace(VadConfig(),**options))
        with self.assertRaises(ValueError):analyze(np.array([np.nan]))


class LocalSTTTests(unittest.TestCase):
    def test_silence_does_not_start_or_download_a_model(self):
        engine=LocalSTT(ProviderSettings());self.addCleanup(engine.close)
        with patch('multiprocessing.context.SpawnProcess.start',side_effect=AssertionError('MODEL_MUST_NOT_START')):
            self.assertFalse(engine.transcribe(packet(np.zeros(32000)))['speech'])
        self.assertFalse(engine.usage['local_diagnostics']['engine_invoked'])

    def test_cancel_stops_the_owned_process(self):
        engine=LocalSTT(replace(ProviderSettings(),stt_timeout=5));self.addCleanup(engine.close);cancel=threading.Event();errors=[]
        def run():
            try:engine.transcribe(packet(),cancel=cancel)
            except ProviderError as exc:errors.append(str(exc))
        with patch('ravora.local_stt._worker',slow_worker):
            thread=threading.Thread(target=run);thread.start();deadline=time.monotonic()+2
            while engine.process is None and time.monotonic()<deadline:time.sleep(.01)
            child=engine.process;self.assertIsNotNone(child);cancel.set();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertFalse(child.is_alive());self.assertEqual(errors,['LOCAL_STT_CANCELLED'])

    def test_hard_deadline_kills_inference_and_never_falls_back_to_cloud(self):
        engine=LocalSTT(replace(ProviderSettings(),stt_timeout=1));self.addCleanup(engine.close);started=time.monotonic()
        with patch('ravora.local_stt._worker',slow_worker),patch('socket.create_connection',side_effect=AssertionError('CLOUD_FORBIDDEN')):
            with self.assertRaisesRegex(ProviderError,'LOCAL_STT_TIMEOUT'):engine.transcribe(packet())
        self.assertLess(time.monotonic()-started,3);self.assertIsNone(engine.process)

    def test_model_cannot_resolve_outside_owned_assets(self):
        with self.assertRaisesRegex(ProviderError,'LOCAL_STT_MODEL_MISSING'):verify_model('/tmp')
        for options in ({'stt_provider':'gemini'},{'stt_model_path':'/tmp/model'},{'stt_model_path':'assets/stt/../private'},{'stt_vad_probability':.1}):
            with self.assertRaises(SettingsError):replace(ProviderSettings(),**options).validate()
