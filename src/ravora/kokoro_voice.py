"""English Twitch speech over the private, warmed Kokoro CUDA socket."""
import logging
import os
from pathlib import Path
import re

from ravora.edge_grpc import pcm_wave
from ravora.providers import ProviderError
from ravora.kokoro_profile import RayEnglishProfile


CONTINUATION_LIMIT = 56

# Prefer punctuation, then the start of a clause or a prepositional phrase.
_BOUNDARY = re.compile(r"""(?P<mark>[.!?,;:…—–]+["'”’)]*)\s+|\s+(?P<next>(?:seen through|made (?:of|from)|filled with|covered (?:in|by)|built (?:from|with)|lit by|lined with|and|but|because|while|when|though|which|where|before|after|with|without|through|until|so that|instead of)\b|[—–]\s+)""", re.I)
_ABBREVIATION = re.compile(r'\b(?:Dr|Mr|Mrs|Ms|Prof|St|vs|e\.g|i\.e|[A-Z])\.$', re.I)


def reply_lead(text):
    """Return a safe first phrase for a long reply, or the complete text."""
    if len(text) < 64:
        return text
    candidates = []
    for match in _BOUNDARY.finditer(text):
        end = match.end('mark') if match.group('mark') else match.start()
        if end > 75:
            break
        head = text[:end]
        if end < 12 or len(text[end:].strip()) < 12 or _ABBREVIATION.search(head):
            continue
        if len(head.split()) < 3 or not any(ch.isalpha() for ch in head):
            continue
        # Rank near-target punctuation above weaker syntactic boundaries.
        distance = max(20 - end, end - 35, 0)
        penalty = 0 if match.group('mark') else 8
        candidates.append((distance + penalty, abs(end - 28), end))
    return text[:min(candidates)[2]] if candidates else text


def request_parts(text):
    """Respect the socket's 500-character limit even for a long remaining passage."""
    while len(text) > 500:
        stops = [m.end() for m in re.finditer(r'[.!?;]["”’\']*(?=\s)', text[:501]) if m.end() <= 500]
        spaces = [m.start() for m in re.finditer(r'\s+', text[:501])]
        cut = next((n for n in reversed(stops) if n >= 200), max(spaces, default=0))
        if cut <= 0:
            raise ProviderError('KOKORO_TOKEN_TOO_LONG')
        yield text[:cut].rstrip()
        text = text[cut:].lstrip()
    if text:
        yield text


def continuation_parts(text):
    """Bound per-request synthesis work without splitting a word or dropping a tail."""
    while len(text) > CONTINUATION_LIMIT:
        choices = []
        for match in _BOUNDARY.finditer(text):
            end = match.end('mark') if match.group('mark') else match.start()
            if end > CONTINUATION_LIMIT:
                break
            if end >= 16 and not _ABBREVIATION.search(text[:end]):
                choices.append((0 if match.group('mark') else 1, abs(40 - end), end))
        if choices:
            cut = min(choices)[2]
        else:
            space = list(re.finditer(r'\s+', text[:CONTINUATION_LIMIT + 1]))
            if space:
                cut = space[-1].start()
            else:
                # Keep an indivisible token intact within the socket's existing limit.
                following = re.search(r'\s', text)
                cut = following.start() if following else len(text)
        yield from request_parts(text[:cut].strip())
        text = text[cut:].lstrip()
    yield from request_parts(text)


class KokoroSpeech(RayEnglishProfile):
    continuation_limit = CONTINUATION_LIMIT
    local = True
    streams_program_pcm = True

    def __init__(self, settings, socket_path=None):
        runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
        self.socket_path = str(socket_path or Path(runtime) / 'ray-kokoro' / 'kokoro.sock')
        self.timeout = settings.timeout
        self.viewer_reply = False
        self.preview_caption = None
        self.fallback_voice = None
        fallback = os.environ.get('RAVORA_KOKORO_FALLBACK_SOCKET')
        if fallback and Path(fallback).is_absolute() and Path(fallback).resolve() != Path(self.socket_path).resolve():
            from copy import copy
            self.fallback_voice = copy(self)
            self.fallback_voice.socket_path = fallback

    def _render_part(self, text):
        try:
            return self._pcm(text)
        except ProviderError as exc:
            if str(exc) != 'LOCAL_TTS_HTTP_503':
                raise
            # _pcm returns only complete, validated PCM. Nothing from this failed
            # request has been published. Never replay an earlier successful part.
            boundaries = list(re.finditer(r'(?<=\S)\s+(?=\S)', text))
            if not boundaries:
                raise
            middle = min(boundaries, key=lambda m: abs(m.start() + m.end() - len(text)))
            left, right = text[:middle.start()], text[middle.end():]
            logging.getLogger(__name__).info('KOKORO_SPLIT_RETRY characters=%d halves=%d,%d',
                                             len(text), len(left), len(right))
            # Direct calls give this part exactly one retry level. Publish both
            # halves together so a failure of the right half cannot repeat the left.
            return self._pcm(left) + self._pcm(right)

    def create(self, text, output, *, on_pcm=None):
        head = reply_lead(text) if self.viewer_reply else text
        rest = text[len(head):].strip()
        sound = bytearray()
        if self.viewer_reply:
            parts = [*continuation_parts(head), *continuation_parts(rest)]
        else:
            parts = list(request_parts(text))
        preview = parts[0] if parts else head
        if len(parts) > 2 and on_pcm:
            from ravora.live_overlay import caption_layout
            preview = ' '.join(caption_layout(text)[1][0].split())
        for index, part in enumerate(parts):
            pcm = self._render_part(part)
            if len(sound) + len(pcm) > 60*48000:
                raise ProviderError('PROGRAM_AUDIO_TOO_LONG')
            sound.extend(pcm)
            if on_pcm and (index == 0 or index < len(parts) - 1):
                self.preview_caption = preview
                try:
                    on_pcm(pcm)
                finally:
                    self.preview_caption = None
        Path(output).write_bytes(pcm_wave(sound, 24000))
        return {'source': 'local-kokoro-cuda', 'voice': self.voice, 'duration': len(sound)/48000}
