import asyncio
import io
import math
from pathlib import Path
import struct
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
import wave

from ravora.conversation import MemoryStore
from ravora.edge_grpc import EdgeClients,EdgeError,pcm_wave,wave_pcm
from ravora.stream_pitch import pitch_shift_pcm


class StreamingPitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_server_uses_configured_pitch_for_chirp(self):
        from ravora.fast_voice_server import FastVoiceServer
        from ravora.settings import load_settings
        settings = load_settings(Path('/nonexistent'), environ={
            'GOOGLE_TTS_PITCH': '1.5', 'GOOGLE_TTS_SPEAKING_RATE': '.96',
            'RAVORA_API_ENABLED': 'true', 'RAVORA_STT_API_ENABLED': 'true',
            'GOOGLE_CLOUD_PROJECT': 'test-project',
        })
        server = FastVoiceServer(settings, None, '.')
        with patch.dict('sys.modules', {'websockets.asyncio.server': NS(serve=None)}), \
             patch.object(EdgeClients, 'open', side_effect=RuntimeError('stop before network')), \
             self.assertRaisesRegex(RuntimeError, 'stop before network'):
            await server.start()
        self.assertEqual(server.clients.stream_pitch, 1.5)
        self.assertEqual(server.clients.voice, 'en-US-Chirp3-HD-Zephyr')
        self.assertEqual(server.clients.speaking_rate, .96)

    def require_live_shifter(self):
        from ravora.stream_pitch import load_live_shifter
        try:load_live_shifter()
        except RuntimeError as exc:self.skipTest(str(exc))

    async def test_shifted_wire_audio_streams_at_the_original_tempo(self):
        self.require_live_shifter()
        samples = [round(7000 * math.sin(2 * math.pi * 240 * i / 24000)) for i in range(24019)]
        pcm = struct.pack(f'<{len(samples)}h', *samples)
        consumed = []
        async def packets():
            for i in range(0, len(pcm), 1394):
                consumed.append(i)
                yield pcm[i:i + 1394]
        for semitones in (0, 2, -3):
            consumed.clear()
            stream = pitch_shift_pcm(packets(), semitones)
            first = await anext(stream)
            self.assertLess(consumed[-1], len(pcm) // 2)
            output = first + b''.join([chunk async for chunk in stream])
            self.assertEqual(len(output), len(pcm))
            if semitones == 0:
                self.assertEqual(output, pcm)
            middle = struct.unpack_from('<12000h', output, 8000)
            hz = 2 * sum(a <= 0 < b for a, b in zip(middle, middle[1:]))
            self.assertAlmostEqual(hz, 240 * 2 ** (semitones / 12), delta=2)

    async def test_short_input_drains_only_on_clean_eof(self):
        self.require_live_shifter()
        async def packets(data, error=None):
            yield data
            if error:
                raise error
        for data in (b'', b'\x00\x10' * 47):
            chunks = [c async for c in pitch_shift_pcm(packets(data), 2)]
            self.assertEqual(sum(map(len, chunks)), len(data))
        failed = pitch_shift_pcm(packets(b'\x00\x10' * 47, OSError()), 2)
        with self.assertRaises(OSError):
            await anext(failed)

    async def test_closing_chirp_response_cancels_rpc_without_playing_tail(self):
        self.require_live_shifter()
        client = EdgeClients({'tts_key': 'test'}, stream_pitch=2)
        client.tts_types = NS(StreamingSynthesizeRequest=NS, StreamingSynthesizeConfig=NS,
            VoiceSelectionParams=NS, StreamingAudioConfig=NS, AudioEncoding=NS(PCM=1), StreamingSynthesisInput=NS)
        response = MagicMock()
        response.__aiter__.return_value = [NS(audio_content=b'\x00\x10' * 3000)]
        async def wire(*, requests, **kwargs):
            requests = [item async for item in requests]
            self.assertEqual(requests[0].streaming_config.voice.name, client.voice)
            config = vars(requests[0].streaming_config.streaming_audio_config)
            self.assertEqual(config, dict(audio_encoding=1, sample_rate_hertz=24000, speaking_rate=.96))
            return response
        client.tts = NS(streaming_synthesize=wire)
        async def texts():
            yield 'Hello.'
        pcm = client.synthesize_stream(texts())
        self.assertTrue(await anext(pcm))
        await pcm.aclose()
        response.cancel.assert_called_once()
        with self.assertRaises(StopAsyncIteration):
            await anext(pcm)


class PCMTests(unittest.TestCase):
    def test_wave_roundtrip_and_rate_rejection(self):
        pcm=b'\x00\x10'*2400;self.assertEqual(wave_pcm(pcm_wave(pcm,24000)),pcm)
        with self.assertRaises(EdgeError):wave_pcm(pcm_wave(pcm,16000))
    def test_endpoint_selection_is_explicit(self):
        self.assertEqual(EdgeClients({},stt_region='asia-southeast1').endpoints['stt'],'asia-southeast1-speech.googleapis.com')
        with self.assertRaises(EdgeError):EdgeClients({},stt_region='automatic')


class GenerationTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_response_and_first_spoken_text_are_distinct(self):
        client=EdgeClients({'gemini_key':'test-only'})
        client.gen_types=NS(Content=NS,Part=NS,GenerateContentRequest=NS,GenerationConfig=NS)
        clock=MagicMock()
        clock.monotonic.return_value=1.

        class Stream:
            async def initial_metadata(self):
                clock.monotonic.return_value=1.2
                return [('server-timing','gfet4t7; dur=182'),('ignored','private-value')]

            async def __aiter__(self):
                for at,thought,value in [(1.3,True,'internal'),(1.6,False,'Spoken.')]:
                    clock.monotonic.return_value=at
                    yield NS(usage_metadata=None,candidates=[NS(index=0,finish_reason=NS(name='STOP'),
                        content=NS(parts=[NS(text=value,thought=thought)]))])

        client.llm=NS(stream_generate_content=AsyncMock(return_value=Stream()))
        timings={}
        with patch('ravora.edge_grpc.time',clock):
            spoken=[part async for part in client.generate('Hello',timings=timings)]
        self.assertEqual(spoken,['Spoken.'])
        self.assertEqual((timings['headers_ms'],timings['first_response_ms'],timings['first_text_ms']),(200.,300.,600.))
        self.assertEqual(timings['gateway_reported_ms'],182.)
        self.assertNotIn('private-value',str(timings))
        client.llm.stream_generate_content.assert_awaited_once()

    async def test_failed_open_has_elapsed_time_but_no_first_text_or_second_call(self):
        client=EdgeClients({'gemini_key':'test-only'})
        client.gen_types=NS(Content=NS,Part=NS,GenerateContentRequest=NS,GenerationConfig=NS)
        client.llm=NS(stream_generate_content=AsyncMock(side_effect=TimeoutError))
        timings={}
        with self.assertRaises(TimeoutError):
            _=[part async for part in client.generate('Hello',timings=timings)]
        self.assertIn('rpc_exit_ms',timings)
        self.assertNotIn('first_text_ms',timings)
        client.llm.stream_generate_content.assert_awaited_once()


class HedgedGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_first_request_is_hedged_and_cancelled(self):
        client=EdgeClients({'gemini_key':'test-only'})
        client.gen_types=NS(Content=NS,Part=NS,GenerateContentRequest=NS,GenerationConfig=NS)
        made=[]

        class Stream:
            def __init__(self,delay,text):self.delay=delay;self.text=text;self.cancelled=False
            def cancel(self):self.cancelled=True
            async def __aiter__(self):
                await asyncio.sleep(self.delay)
                yield NS(usage_metadata=None,candidates=[NS(index=0,finish_reason=NS(name='STOP'),
                    content=NS(parts=[NS(text=self.text,thought=False)]))])

        async def open_stream(**kwargs):
            stream=Stream(5 if not made else 0,'slow' if not made else 'fast');made.append(stream);return stream
        client.llm=NS(stream_generate_content=open_stream)
        timings={}
        with patch('ravora.edge_grpc.HEDGE_AFTER',.05):
            spoken=[part async for part in client.generate('Hello',timings=timings)]
        self.assertEqual(spoken,['fast'])
        self.assertEqual((timings['hedged_requests'],timings['hedge_winner']),(1,1))
        self.assertTrue(made[0].cancelled)
        self.assertFalse(made[1].cancelled)

    async def test_fast_request_is_not_hedged(self):
        client=EdgeClients({'gemini_key':'test-only'})
        client.gen_types=NS(Content=NS,Part=NS,GenerateContentRequest=NS,GenerationConfig=NS)

        class Stream:
            def cancel(self):pass
            async def __aiter__(self):
                yield NS(usage_metadata=None,candidates=[NS(index=0,finish_reason=NS(name='STOP'),
                    content=NS(parts=[NS(text='Hi.',thought=False)]))])
        client.llm=NS(stream_generate_content=AsyncMock(return_value=Stream()))
        timings={}
        self.assertEqual([part async for part in client.generate('Hello',timings=timings)],['Hi.'])
        self.assertEqual(timings['hedged_requests'],0)
        client.llm.stream_generate_content.assert_awaited_once()
