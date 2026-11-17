"""Local English speech and phoneme times from the SAME Flite utterance.

Only public, opaque-pointer libflite APIs are used. No model download or API call.
The phonemes describe synthesized speech; they are not recognition of arbitrary audio.
"""
import ctypes as C
import hashlib
import json
import math
from pathlib import Path
import subprocess
import wave


def viseme_parts(phone):
    """Five approximate vowel shapes, plus closed lips and a neutral consonant."""
    phone = phone.lower().rstrip('012')
    diphthongs = {'ay': ('A', 'I'), 'aw': ('A', 'U'), 'oy': ('O', 'I'),
                  'ow': ('O', 'U'), 'ey': ('E', 'I')}
    if phone in diphthongs:
        return diphthongs[phone]
    for shape, phones in [('closed', {'pau', 'sil', 'm', 'b', 'p'}),
                          ('A', {'aa', 'ae', 'ah', 'ax'}),
                          ('I', {'iy', 'ih', 'y'}),
                          ('U', {'uw', 'uh', 'w'}),
                          ('E', {'eh'}), ('O', {'ao', 'er', 'r'})]:
        if phone in phones:
            return (shape,)
    return ('N',)


# Mouth planning. The reply text is known before playout, so the whole mouth track is built
# ahead of time from the synthesizer's own phones, lexical stress, pitch accent and word class:
# - only vowels, lip closures (m/b/p), f/v and w/r rounding become mouth keys; other consonants are
#   absorbed between neighbouring vowels (with a small constriction when they are long),
# - opening follows stress: accented stressed vowels open most, function words / schwa least,
# - keys shorter than 70 ms are merged, so shapes change about 5-8 times per second,
# - the whole track leads the audio by 60 ms (lips and jaw move before the sound).
MOUTH_LEAD = .06
MOUTH_MIN_KEY = .07


def _vowel_amount(phone, info):
    base = phone.rstrip('012')
    function_word = info.get('gpos') not in (None, 'content')
    # Fractions of the drawn (fully articulated) vowel: conversational speech stays at 6-8 tenths.
    if base == 'ax' or (function_word and info.get('accented') != '1'):
        return .34
    if info.get('stress') == '1' and info.get('accented') == '1':
        return .8
    if info.get('stress') == '1':
        return .64
    return .48


def plan_mouth(phones, *, lead=MOUTH_LEAD, minimum_key=MOUTH_MIN_KEY):
    """phones: [{'phone','start','end', optional 'vc','stress','accented','gpos'}] -> keys.

    Each key holds `shape` (A/I/U/E/O or 'closed') at `amount` (fraction of the drawn vowel)
    from `start` to `end`; the renderer eases between consecutive keys."""
    keys = []
    for p in phones:
        name, start, end = p['phone'].lower(), p['start'], p['end']
        dur = end - start
        base = name.rstrip('012')
        vowel = p.get('vc') == '+' or base in {'aa', 'ae', 'ah', 'ax', 'iy', 'ih', 'uw', 'uh', 'eh', 'ao', 'er', 'ey', 'ay', 'aw', 'oy', 'ow'}
        if base in ('pau', 'sil'):
            keys.append({'shape': 'closed', 'amount': 0., 'start': start, 'end': end, 'phone': base, 'kind': 'rest'})
        elif base in ('m', 'b', 'p'):
            keys.append({'shape': 'closed', 'amount': 0., 'start': start, 'end': end, 'phone': base, 'kind': 'closed'})
        elif base in ('f', 'v'):
            keys.append({'shape': 'I', 'amount': .22, 'start': start, 'end': end, 'phone': base, 'kind': 'cons'})
        elif base in ('w', 'r'):
            keys.append({'shape': 'U', 'amount': .3, 'start': start, 'end': end, 'phone': base, 'kind': 'cons'})
        elif vowel:
            amount = _vowel_amount(name, p)
            shapes = viseme_parts(name)
            if len(shapes) == 1:
                keys.append({'shape': shapes[0] if shapes[0] in 'AIUEO' else 'A', 'amount': amount, 'start': start, 'end': end, 'phone': base, 'kind': 'vowel'})
            else:
                split = start + dur * .58
                keys.append({'shape': shapes[0], 'amount': amount, 'start': start, 'end': split, 'phone': base, 'kind': 'vowel'})
                keys.append({'shape': shapes[1], 'amount': amount * .8, 'start': split, 'end': end, 'phone': base, 'kind': 'vowel'})
        else:
            keys.append({'shape': None, 'amount': None, 'start': start, 'end': end, 'phone': base})
    # Absorb plain consonants: long ones become a light constriction towards the next vowel.
    out = []
    for i, k in enumerate(keys):
        if k['shape'] is not None:
            out.append(k); continue
        if k['end'] - k['start'] < .09:
            continue
        nxt = next((x for x in keys[i + 1:] if x['shape'] not in (None, 'closed')), None)
        prv = next((x for x in reversed(keys[:i]) if x['shape'] not in (None, 'closed')), None)
        ref = nxt or prv
        if ref:
            out.append({'shape': ref['shape'], 'amount': round(.4 * ref['amount'], 3), 'start': k['start'], 'end': k['end'], 'phone': k['phone'], 'kind': 'cons'})
    # Merge short keys. Vowels survive and absorb a short consonant neighbour; consonant keys merge
    # into the next vowel (anticipation) or the previous one. Closures are never merged away.
    merged = []
    for k in out:
        if merged and k['shape'] == merged[-1]['shape'] and k['kind'] == merged[-1]['kind'] and abs(k['amount'] - merged[-1]['amount']) < .12:
            merged[-1]['end'] = k['end']; merged[-1]['amount'] = max(merged[-1]['amount'], k['amount']); continue
        merged.append(dict(k))
    def absorb(into, gone):
        into['start'], into['end'] = min(into['start'], gone['start']), max(into['end'], gone['end'])
    changed = True
    while changed:
        changed = False
        for i, k in enumerate(merged):
            if k['end'] - k['start'] >= minimum_key or k['kind'] in ('closed', 'rest'):
                continue
            left = merged[i - 1] if i > 0 else None
            right = merged[i + 1] if i + 1 < len(merged) else None
            if k['kind'] == 'vowel':
                cons = [n for n in (left, right) if n and n['kind'] == 'cons']
                if cons:
                    n = min(cons, key=lambda x: x['end'] - x['start'])
                    absorb(k, n); merged.remove(n); changed = True; break
                vowels = [n for n in (left, right) if n and n['kind'] == 'vowel']
                if vowels:
                    n = max(vowels, key=lambda x: x['amount'])
                    absorb(n, k); merged.remove(k); changed = True; break
            else:
                vowels = [n for n in (right, left) if n and n['kind'] == 'vowel']
                if vowels:
                    absorb(vowels[0], k); merged.remove(k); changed = True; break
    # Closures shorter than 50 ms still need to read as a closure.
    for k in merged:
        if k['shape'] == 'closed' and k['end'] - k['start'] < .05:
            mid = (k['start'] + k['end']) / 2
            k['start'], k['end'] = mid - .025, mid + .025
    # Times may be negative: the first shape starts forming before the audio does.
    return [{'shape': k['shape'], 'amount': round(k['amount'], 3),
             'start': round(k['start'] - lead, 6), 'end': round(k['end'] - lead, 6),
             'phone': k['phone']} for k in merged if k['end'] - k['start'] > 1e-4]


def align_segments(segments, trim_seconds, duration):
    phones, visemes = [], []
    previous = 0.
    for segment in segments:
        phone, end, info = (segment + ({},))[:3] if isinstance(segment, tuple) else segment
        if not math.isfinite(end) or end <= previous:
            raise ValueError('INVALID_PHONEME_TIMELINE')
        start, stop = max(0., previous - trim_seconds), min(duration, end - trim_seconds)
        previous = end
        if stop <= start:
            continue
        phones.append({'phone': phone, 'start': round(start, 6), 'end': round(stop, 6), **info})
        shapes = viseme_parts(phone)
        split = start + (stop - start) * .58
        bounds = [start, stop] if len(shapes) == 1 else [start, split, stop]
        for i, shape in enumerate(shapes):
            visemes.append({'shape': shape, 'phone': phone,
                            'start': round(bounds[i], 6), 'end': round(bounds[i + 1], 6)})
    if not phones or phones[-1]['end'] < duration - .05:
        raise ValueError('PHONEME_WAVE_DURATION_MISMATCH')
    return phones, visemes


class LocalSpeech:
    def __init__(self):
        self.lib = C.CDLL('libflite.so.1', mode=C.RTLD_GLOBAL)
        self.voice_lib = C.CDLL('libflite_cmu_us_slt.so.1', mode=C.RTLD_GLOBAL)
        def bind(lib, name, result, args):
            f = getattr(lib, name)
            f.restype, f.argtypes = result, args
            return f
        p, s = C.c_void_p, C.c_char_p
        bind(self.lib, 'flite_init', C.c_int, [])()
        self.voice = bind(self.voice_lib, 'register_cmu_us_slt', p, [s])(None)
        if not self.voice:
            raise ValueError('LOCAL_VOICE_UNAVAILABLE')
        self.unregister = bind(self.voice_lib, 'unregister_cmu_us_slt', None, [p])
        self.synth = bind(self.lib, 'flite_synth_text', p, [s, p])
        self.relation = bind(self.lib, 'utt_relation', p, [p, s])
        self.head = bind(self.lib, 'relation_head', p, [p])
        self.next = bind(self.lib, 'item_next', p, [p])
        self.item_as = bind(self.lib, 'item_as', p, [p, s])
        self.parent = bind(self.lib, 'item_parent', p, [p])
        self.name = bind(self.lib, 'item_feat_string', s, [p, s])
        self.end = bind(self.lib, 'item_feat_float', C.c_float, [p, s])
        self.utt_wave = bind(self.lib, 'utt_wave', p, [p])
        self.save = bind(self.lib, 'cst_wave_save_riff', C.c_int, [p, s])
        self.delete = bind(self.lib, 'delete_utterance', None, [p])
        self.feature = bind(self.lib, 'ffeature_string', s, [p, s])

    def close(self):
        if self.voice:
            self.unregister(self.voice)
            self.voice = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def pronunciation(self, word):
        """Return the phone names and their relative durations for an English word."""
        utterance = self.synth(word.encode('utf-8'), self.voice)
        if not utterance:
            raise ValueError('LOCAL_SYNTHESIS_FAILED')
        try:
            item = self.head(self.relation(utterance, b'Segment'))
            phones, previous = [], 0.
            while item:
                phone = self.name(item, b'name').decode('ascii')
                end = self.end(item, b'end')
                if phone not in ('pau', 'sil') and end > previous:
                    phones.append((phone, end - previous))
                previous = end
                item = self.next(item)
            return phones
        finally:
            self.delete(utterance)

    def create(self, text, output: Path):
        if not self.voice or not isinstance(text, str) or not 0 < len(text) <= 1000 or '\0' in text:
            raise ValueError('INVALID_LOCAL_SPEECH_INPUT')
        output = Path(output)
        native = output.with_suffix('.native.wav')
        utterance = self.synth(text.encode('utf-8'), self.voice)
        if not utterance:
            raise ValueError('LOCAL_SYNTHESIS_FAILED')
        segments = []
        token_ids = {}
        try:
            relation = self.relation(utterance, b'Segment')
            if not relation:
                raise ValueError('MISSING_SYNTHESIS_PHONEMES')
            item = self.head(relation)
            while item:
                if len(segments) >= 2000:
                    raise ValueError('PHONEME_BUDGET_EXCEEDED')
                info = {}
                for key, path in (('vc', b'ph_vc'), ('stress', b'R:SylStructure.parent.stress'),
                                  ('accented', b'R:SylStructure.parent.accented'),
                                  ('gpos', b'R:SylStructure.parent.parent.gpos')):
                    value = self.feature(item, path)
                    info[key] = value.decode('ascii', 'replace') if value else None
                syl_item = self.item_as(item, b'SylStructure')
                syllable = self.parent(syl_item) if syl_item else None
                word = self.parent(syllable) if syllable else None
                token_word = self.item_as(word, b'Token') if word else None
                token = self.parent(token_word) if token_word else None
                if token:
                    if token not in token_ids:
                        token_ids[token] = len(token_ids)
                    info['token_index'] = token_ids[token]
                    info['token'] = self.name(token, b'name').decode('utf-8', 'replace')
                segments.append((self.name(item, b'name').decode('ascii'), self.end(item, b'end'), info))
                item = self.next(item)
            waveform = self.utt_wave(utterance)
            if not waveform or self.save(waveform, str(native).encode()) != 0:
                raise ValueError('LOCAL_WAVE_SAVE_FAILED')
        finally:
            self.delete(utterance)
        with wave.open(str(native)) as reader:
            rate, channels, width = reader.getframerate(), reader.getnchannels(), reader.getsampwidth()
        if channels != 1 or width != 2 or not 8000 <= rate <= 48000:
            raise ValueError('LOCAL_WAVE_FORMAT')
        # Crop only the initial silence segment, leaving 15 ms before the first phone.
        # Use the exact sample offset for BOTH audio and phonemes; never trim independently.
        trim = max(0., segments[0][1] - .015) if segments[0][0] in ('pau', 'sil') else 0.
        trim_samples = int(trim * rate)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-threads', '1',
                        '-filter_threads', '1', '-i', str(native), '-af',
                        f'atrim=start_sample={trim_samples},asetpts=PTS-STARTPTS',
                        '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', str(output)],
                       check=True, timeout=15)
        with wave.open(str(output)) as reader:
            duration = reader.getnframes() / reader.getframerate()
        phones, visemes = align_segments(segments, trim_samples / rate, duration)
        report = {'source': 'flite-same-utterance-segments', 'voice': 'local flite slt',
                  'duration': duration, 'trim_seconds': trim_samples / rate,
                  'audio_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                  'phones': phones, 'visemes': visemes, 'mouth_plan': plan_mouth(phones)}
        output.with_suffix('.speech.json').write_text(json.dumps(report, indent=2) + '\n')
        return report
