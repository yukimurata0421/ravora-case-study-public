"""English Twitch speech over the private, warmed Kokoro CUDA socket."""
import os
from pathlib import Path
import re

from ravora.edge_grpc import pcm_wave
from ravora.providers import ProviderError
from ravora.kokoro_profile import RayEnglishProfile


CONTINUATION_LIMIT = 165

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
    # A long tail can outlast its playing lead. Prepare the next phrase first,
    # and let the same model synthesize subsequent phrases during playout.
    while len(text) > CONTINUATION_LIMIT:
        boundaries = []
        for match in _BOUNDARY.finditer(text):
            end = match.end('mark') if match.group('mark') else match.start()
            if end > 110:
                break
            if 32 <= end and not _ABBREVIATION.search(text[:end]):
                penalty = 0 if match.group('mark') else 15
                boundaries.append((abs(end-72) + penalty, end))
        if boundaries:
            cut = min(boundaries)[1]
        else:
            cut = text.rfind(' ', 1, 101)
            if cut < 1:
                break
        yield text[:cut].strip()
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

    def create(self, text, output, *, on_pcm=None):
        head = reply_lead(text) if self.viewer_reply else text
        rest = text[len(head):].strip()
        sound = bytearray()
        parts = [*request_parts(head), *continuation_parts(rest)]
        preview = head
        if len(parts) > 2 and on_pcm:
            from ravora.live_overlay import caption_layout
            preview = ' '.join(caption_layout(text)[1][0].split())
        for index, part in enumerate(parts):
            pcm = self._pcm(part)
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
