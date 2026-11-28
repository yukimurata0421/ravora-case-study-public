"""Bind a voice and a validated first-phrase preview to one studio turn."""
from copy import copy
import os
from pathlib import Path
import tempfile
import time
import wave

from ravora.aivis_voice import KanonSpeech
from ravora.chat_selection import VIEWER_CHAT_SOURCES
from ravora.kokoro_voice import KokoroSpeech
from ravora.providers import GeminiText, ProviderError


def profile_live_turn(program, bridge, comment, language):
    from ravora.live_program import youtube_reply_language

    if not program.reply_profiles:
        return
    viewer = comment['source'] in VIEWER_CHAT_SOURCES
    if viewer:
        language = youtube_reply_language(comment['comment'], language)
    elif comment['source'] == 'ray-initiative':
        language = program.initiative_language
    controller = bridge.controller
    voices = {key: profile[2] for key, profile in program.reply_profiles.items()}
    controller.s, text, _ = program.reply_profiles[language]
    controller.text = copy(text)
    controller.reply_playable = None
    if viewer and 'receipt' in comment:
        receipt = comment['receipt']
        controller.reply_playable = lambda: program.pipeline.inbox.playable(receipt)
    if viewer and isinstance(controller.text, GeminiText):
        controller.text.prompt = controller.text.viewer_prompt + (
            '\nSpeak the answer in the first phrase. Casual Japanese: one short complete sentence, '
            'usually 8–18 characters. A yes/no or preference starts with your position. '
            'Why questions start with the concrete reason. Finish the short answer with a period. '
            'Give an example or more detail when asked; an unknown needs only one short clause. '
            'Honor single-value requests. For a recap name the main subjects. '
            'Ask for missing facts rather than guessing. No filler.'
        )
    if viewer and isinstance(voices['ja'], KanonSpeech):
        voices['ja'] = copy(voices['ja'])
        voices['ja'].viewer_reply = True
    english = voices['en']
    if isinstance(english, KokoroSpeech):
        controller.program_pcm = program._receive_pcm
        if viewer:
            # Prefetched replies may run alongside a playing turn. Keep both
            # the preview caption and its receiver local to this generation.
            english = voices['en'] = copy(english)
            english.viewer_reply = True
            def receive(turn, full_text, pcm):
                program._receive_pcm(turn, english.preview_caption or full_text, pcm)
            controller.program_pcm = receive
            if isinstance(controller.text, GeminiText):
                controller.text.prompt += ('\nFor a longer English viewer reply, aim to open with a natural '
                                           'standalone sentence or clause of about 20 to 35 characters. '
                                           'Use punctuation at the break. Keep short answers short.')
    controller.speech = voices[language]
    controller.language_voices = (voices if any(getattr(v, 'local', False) for v in voices.values())
                                 and (viewer or comment['source'] == 'live-host') else None)


def refresh_english_voice(program):
    """Swap the default for new turns without touching in-flight speech."""
    if not program.reply_profiles or program.pipeline.s.comment_source != 'twitch':
        return
    from ravora.settings import read_env
    from ravora.live_voice import LiveChirpSpeech
    from ravora.providers import WaveNetSpeech

    settings, text, _ = program.reply_profiles['en']
    env_file = getattr(program, 'voice_env_file', Path('.env'))
    choice = os.environ.get('RAVORA_TWITCH_EN_TTS') or read_env(env_file).get('RAVORA_TWITCH_EN_TTS', 'kokoro')
    if choice == 'kokoro':
        voice = KokoroSpeech(settings)
    elif choice == 'google':
        voice = LiveChirpSpeech(settings) if settings.voice_profile == 'fast_streaming' else WaveNetSpeech(settings)
    else:
        raise ValueError('INVALID_TWITCH_EN_TTS')
    program.reply_profiles = {**program.reply_profiles, 'en': (settings, text, voice)}
    if type(program)._profile is not profile_live_turn:
        program._profile = profile_live_turn.__get__(program)
    print(f'TWITCH_VOICE_APPLIED en={voice.voice} ja={program.reply_profiles["ja"][2].voice}', flush=True)


def warm_twitch_voices(profiles, *, allow_unavailable=False):
    """Exercise each selected local voice before the publisher opens."""
    import array
    from ravora.providers import GeminiStreamConnection

    transport = getattr(profiles['en'][1], 'http', None)
    if isinstance(transport, GeminiStreamConnection):
        transport.warm()

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'ready.wav'
        deadline = time.monotonic() + 8
        for code, phrase in (('en', 'Ray ready. English TTS.'), ('ja', 'レイ、日本語準備完了。')):
            voice = profiles[code][2]
            if not getattr(voice, 'local', False):
                continue
            try:
                while not Path(voice.socket_path).is_socket() and time.monotonic() < deadline:
                    time.sleep(.15)
                start = time.monotonic()
                timing = voice.create(phrase, path)
                render_seconds = time.monotonic() - start
                with wave.open(str(path)) as wav:
                    pcm = array.array('h', wav.readframes(wav.getnframes()))
                    if not pcm or max(map(abs, pcm)) <= 64:
                        raise ProviderError('SILENT_TTS_WARMUP')
                    if isinstance(voice, KanonSpeech) and timing.get('duration', 0) > 0:
                        voice.warm_render_ratio = render_seconds / timing['duration']
            except (OSError, ProviderError, wave.Error, EOFError) as exc:
                if not allow_unavailable:
                    raise
                # The voice services also warm before exposing their sockets.
                # An unavailable voice must not take down the live publisher.
                print(f'TWITCH_VOICE_WARM_DEFERRED {code}={voice.voice} {type(exc).__name__}', flush=True)
                continue
            from ravora.spoken_language import prepare_recorded_replies
            try:
                prepare_recorded_replies(code, voice)
            except (OSError, ValueError, ProviderError, wave.Error, EOFError) as exc:
                # A missing recording still uses the selected voice to speak the fixed text.
                print(f'TWITCH_LANGUAGE_RECORDING_DEFERRED {code} {type(exc).__name__}', flush=True)
            print(f'TWITCH_VOICE_WARM {code}={voice.voice}', flush=True)
