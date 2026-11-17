"""Word timestamps from the actual TTS; estimated intraword phones from a local reference."""
import bisect
import html
import math
import re

from ravora.speech import plan_mouth, viseme_parts


def marked_input(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 1000:
        raise ValueError('INVALID_MARKED_TEXT')
    tokens = text.split()
    ssml = '<speak>' + ' '.join(f'<mark name="w{i}"/>'+html.escape(token) for i,token in enumerate(tokens))
    # Audio after the final mark makes the end event observable without another adjacent mark.
    ssml += '<mark name="utterance_end"/><break time="80ms"/></speak>'
    if len(ssml.encode()) > 4500:
        raise ValueError('MARKED_INPUT_TOO_LARGE')
    return ssml, tokens


def retime_reference(reference, tokens, timepoints, duration):
    """Anchor each word to the synthesized WaveNet, then interpolate within that word.

    This is an estimate, not provider phoneme timestamps or acoustic forced alignment.
    Missing/duplicate/non-monotonic marks are rejected instead of using unrelated timings.
    """
    marks = {}
    for point in timepoints:
        name, value = point['markName'], point.get('timeSeconds', 0.)
        if name in marks or type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= duration:
            raise ValueError('INVALID_OR_DUPLICATE_WORD_MARK')
        marks[name] = float(value)
    expected = {'w'+str(i) for i in range(len(tokens))} | {'utterance_end'}
    if set(marks) != expected:
        raise ValueError('MISSING_OR_UNEXPECTED_WORD_MARK')
    words = {}
    for phone in reference['phones']:
        if 'token_index' in phone:
            words.setdefault(phone['token_index'], phone)
    normalize = lambda s: re.sub(r"^[^\w]+|[^\w]+$", '', s).casefold()
    if list(words) != list(range(len(tokens))) or any(normalize(words[i]['token']) != normalize(token) for i,token in enumerate(tokens)):
        raise ValueError('REFERENCE_TOKEN_MISMATCH')
    ref_end = max(p['end'] for p in reference['phones'] if p['phone'] not in ('pau', 'sil'))
    src = [words[i]['start'] for i in range(len(tokens))] + [ref_end]
    dst = [marks['w'+str(i)] for i in range(len(tokens))] + [marks['utterance_end']]
    if any(b <= a for a,b in zip(src,src[1:])) or any(b <= a for a,b in zip(dst,dst[1:])):
        raise ValueError('NON_MONOTONIC_WORD_MARKS')
    def warp(t):
        if t <= src[0]:
            return max(0., dst[0] - (src[0]-t))
        if t >= src[-1]:
            return min(duration, dst[-1] + t-src[-1])
        i = bisect.bisect_right(src,t)-1
        return dst[i] + (t-src[i])/(src[i+1]-src[i])*(dst[i+1]-dst[i])
    phones, visemes = [], []
    for original in reference['phones']:
        start,end = warp(original['start']),warp(original['end'])
        if end-start <= .00001:
            continue
        phone = {**original,'start':round(start,6),'end':round(end,6)}
        phones.append(phone)
        shapes = viseme_parts(phone['phone'])
        bounds = [start,end] if len(shapes)==1 else [start,start+(end-start)*.58,end]
        for i,shape in enumerate(shapes):
            visemes.append({'shape':shape,'phone':phone['phone'],'start':round(bounds[i],6),'end':round(bounds[i+1],6)})
    return {'phones':phones,'visemes':visemes,'mouth_plan':plan_mouth(phones),
            'word_anchors':[{'token':token,'reference_seconds':src[i],'wave_seconds':dst[i]} for i,token in enumerate(tokens)],
            'word_timepoints_source':'Google SSML_MARK from this waveform',
            'intraword_phone_times':'estimated by interpolation of Flite reference; not provider phonemes'}
