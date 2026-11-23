"""Use the conversation console's Chirp stream for a validated on-air sentence."""
import asyncio
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path

from ravora.edge_grpc import EdgeClients, pcm_wave
from ravora.fast_voice import error_code
from ravora.providers import GeminiText, ProviderError, WaveNetSpeech
from ravora.settings import default_wavenet_voice, streaming_voice


def live_reply_profiles(settings):
    profiles = {}
    fast = settings.voice_profile == 'fast_streaming'
    for code, locale in (('ja', 'ja-JP'), ('en', 'en-US')):
        localized = settings
        if (fast and settings.language != locale) or settings.language[:2] != code:
            localized = replace(settings, language=locale, voice=default_wavenet_voice(locale)).validate()
        speech = LiveChirpSpeech(localized) if fast else WaveNetSpeech(localized)
        profiles[code] = localized, GeminiText(localized), speech
    return profiles


class LiveChirpSpeech:
    streams_program_pcm = True

    def __init__(self, settings):
        self.s = settings
        self.voice = streaming_voice(settings.voice_tts_voice, settings.language)

    async def _pcm(self, text, on_pcm):
        import grpc
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import texttospeech_v1 as tts
        from google.cloud.texttospeech_v1.services.text_to_speech.transports import TextToSpeechGrpcAsyncIOTransport

        s = self.s
        engine = EdgeClients({'tts_key': s.tts_key}, tts_region=s.voice_tts_region,
                             language=s.language, voice=self.voice, speaking_rate=s.speaking_rate,
                             stream_pitch=s.pitch)
        async def sentences():
            yield text

        endpoint = engine.endpoints['tts']
        async with grpc.aio.secure_channel(endpoint, grpc.ssl_channel_credentials(),
                                           options=(('grpc.enable_retries', 0),)) as channel:
            engine.tts_types = tts
            engine.tts = tts.TextToSpeechAsyncClient(transport=TextToSpeechGrpcAsyncIOTransport(
                channel=channel, credentials=AnonymousCredentials()))
            sound = bytearray()
            async with aclosing(engine.synthesize_stream(sentences())) as packets:
                async for packet in packets:
                    if len(packet) % 2:
                        raise ProviderError('INVALID_TTS_RESULT')
                    if len(sound) + len(packet) > 60 * 24000 * 2:
                        raise ProviderError('PROGRAM_AUDIO_TOO_LONG')
                    sound.extend(packet)
                    if packet and on_pcm:
                        on_pcm(packet)
            if not sound:
                raise ProviderError('INVALID_TTS_RESULT')
            return sound

    def create(self, text, output, *, on_pcm=None):
        try:
            pcm = asyncio.run(self._pcm(text, on_pcm))
        except Exception as exc:
            raise ProviderError(error_code(exc)) from None
        Path(output).write_bytes(pcm_wave(pcm, 24000))
        return {'source':'google-chirp3-stream', 'voice':self.voice, 'duration':len(pcm)/48000,
                'lip_sync':'decoded-waveform-spectral-heuristic'}
