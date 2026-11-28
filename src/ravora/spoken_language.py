"""Validate an entire spoken reply before the studio releases any PCM or caption."""
import array
import hashlib
import io
from pathlib import Path
import re
import tempfile
import unicodedata
import wave


from .viewer_recall import CLARIFICATIONS


JAPANESE = re.compile(r'[ぁ-ヿ㐀-鿿々ｦ-ﾟ]')
# Recognize direct Russian requests even if the model omits its optional intent.
_RUSSIAN_REQUEST = re.compile(
    r'^\s*(?:пожалуйста[, ]+)?(?:(?:ты\s+|вы\s+)?(?:можешь|можете)\s+|можно\s+)?'
    r'(?:говор(?:и|ить|ишь|ите)|поговор(?:им|ить)|отве(?:ть|тить|чай)|напиши|'
    r'перей(?:ди|ти|д[её]м))\s+(?:(?:со мной|пожалуйста)\s+)?'
    r'(?:по[\s-]*русски|на\s+русском)(?:\s*[,;.!?]?\s*я не понимаю на других языках)?[?!. ]*', re.I)
_ENGLISH_REQUEST = re.compile(
    r'(?:can|could|would) you (?:please )?(?:speak|reply|answer|talk)(?: to me)? (?:in )?'
    r'(?:Russian|Spanish|French|German|Chinese|Korean|Italian|Portuguese|Arabic|Hindi)[?!. ]*', re.I)
_JAPANESE_REQUEST = re.compile(
    r'(?:ロシア|フランス|ドイツ|スペイン|イタリア|ポルトガル|韓国|中国|アラビア)語で'
    r'(?:話して(?:くれる|ください)?|話せ(?:る|ますか)|答えて(?:くれる|ください)?)[？?！!。 ]*')


def explicit_unsupported_request(comment):
    # Full requests only. Mentions, quotations and mixed requests still go to Gemini.
    text = comment.strip()
    return bool(_ENGLISH_REQUEST.fullmatch(text) or _RUSSIAN_REQUEST.fullmatch(text)
                or _JAPANESE_REQUEST.fullmatch(text))

FIXED = {
    'recall': {'en': 'Could you remind me which detail you mean?', 'ja': 'どの話か、もう一度教えて？'},
    'available': {'en': 'I can speak English or Japanese.', 'ja': '英語か日本語で話せるよ。'},
    'clarify': {'en': 'Could you put that another way?', 'ja': '別の言い方で聞いてもらえる？'},
}
ASSETS = Path('state/tts/language-replies')

# A name or acronym alone is common in chat. Pin only positive language cues.
_ENGLISH_CUES = frozenset(
    'ah are can could did do does how is it my not that the this what when where which who why would you your'.split())
_LANGUAGE_CONTEXT = re.compile(
    r'\b(?:speak|language|translate|translation|english|japanese|russian|spanish|french|german|chinese|korean)\b'
    r'|\b(?:reply|answer|respond|write|say)\b.{0,40}\bin\b|語|翻訳|通訳|[「」『』“”"\x60@#<>]'
    r'|https?://|www\.|(?:^|\s)\x27|\x27(?:$|\s)', re.I)
_JAPANESE_CUE = re.compile(r'です|ます|ません|だった|だよ|だね|教えて|ありがとう|こんにちは|こんばんは|'
                          r'おはよう|なぜ|なんで|どう(?:し|だ|な)|[ぁ-ゖ].+[はをが]\s*[?？]$')
_JP_NAME = re.compile(JAPANESE.pattern + '+')
_VOICE_NAMES = {'花音': 'Kanon', 'アイビススピーチ': 'AivisSpeech'}


def _english_words(text):
    if any(ch.isalpha() and not ch.isascii() for ch in text):
        return False
    words = re.findall(r'\b[A-Za-z]+\b', text)
    return any(w.islower() for w in words) and len({w.lower() for w in words} & _ENGLISH_CUES) >= 2


def clear_reply_language(comment):
    text = unicodedata.normalize('NFKC', comment)
    if _LANGUAGE_CONTEXT.search(text):
        return None
    if _JAPANESE_CUE.search(text) and all(JAPANESE.fullmatch(ch) for ch in text if ch.isalpha()):
        return 'ja'
    return 'en' if _english_words(text) else None


def normalize_voice_name(text, language):
    if language != 'en' or text.isascii():
        return text
    names = list(_JP_NAME.finditer(text))
    if len(names) != 1 or names[0][0] not in _VOICE_NAMES:
        return text
    start, end = names[0].span()
    before, after = text[:start], text[end:]
    if before[-1:].isalnum() or after[:1].isalnum():
        return text
    if not _english_words(before + ' ' + after):
        return text
    return before + _VOICE_NAMES[names[0][0]] + after


def readable(text, language):
    """Check scripts the selected phonemizer can read, preserving Latin model names."""
    if language == 'en' and text.isascii():
        return True
    has_japanese = bool(JAPANESE.search(text))
    if language == 'en' and has_japanese:
        return False
    for ch in text:
        if ch.isascii() or not ch.isalpha() or JAPANESE.fullmatch(ch):
            continue
        if 'LATIN' not in unicodedata.name(ch, ''):
            return False
    if language == 'en' or has_japanese:
        return True
    # A number, acronym or model ID can be a complete Japanese answer.
    token = text.strip(' .!?。！？')
    return (' ' not in token and (any(ch.isdigit() for ch in token) or token.isupper()))


def japanese_reply_parts(text):
    """Keep quotations intact and join an introductory filler to its answer."""
    closing = {'「':'」', '『':'』', '（':'）', '(':')', '“':'”'}
    stack = []
    clause_start = 0
    for boundary in re.finditer(r'[「『（(“]|[」』）)”]|、|[。！？]+[」』）)”]*', text):
        token = boundary.group()
        if token in closing:
            stack.append(closing[token])
            continue
        for ch in token:
            if stack and ch == stack[-1]:
                stack.pop()
        if not stack and token == '、':
            # Coordinated subject/value phrases can stand on their own. Leave
            # introductory conditions, quotations and simple noun lists intact.
            lead = text[:boundary.end()]
            tail = text[boundary.end():].lstrip()
            if (re.fullmatch(r'[^、。！？「」『』（）]{1,10}[はが][^、。！？「」『』（）]{1,12}(?:で|だし|ですし)、', lead)
                    and re.match(r'[^、。！？「」『』（）]{1,10}[はが]', tail)):
                return [lead, tail]
        if stack or token[0] not in '。！？':
            continue
        lead = text[:boundary.end()]
        tail = text[boundary.end():].lstrip()
        if token[-1] in closing.values() and tail.startswith(('と','って','の','に','が','を','は')):
            continue
        sentence = text[clause_start:boundary.start()].strip()
        clause_start = boundary.end()
        if tail and sentence not in ('うん','はい','そうだね','なるほど','えっと'):
            return [lead, tail]
    return [text]


class RecordedReply:
    local = True

    def __init__(self, voice, text, wav, duration):
        self.voice, self.text, self.wav, self.duration = voice, text, wav, duration

    def create(self, text, output):
        if text != self.text:
            raise ValueError('WRONG_RECORDED_REPLY')
        Path(output).write_bytes(self.wav)
        return {'source': 'recorded-language-reply', 'duration': self.duration}


def _recording_path(reason, language, voice, text):
    if reason in CLARIFICATIONS:
        # A wording change must never reuse the old recording with a new caption.
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
        return ASSETS / f'{voice}-{language}-{reason}-{digest}.wav'
    return ASSETS / f'{voice}-{reason}.wav'


def fixed_reply(reason, language, original_voice):
    if reason in CLARIFICATIONS:
        text = CLARIFICATIONS[reason][language]
        if language != 'ja' or getattr(original_voice, 'voice', None) != 'ja-JP-Aivis-Kanon-Normal':
            return text, original_voice
    else:
        text = FIXED[reason][language]
    voice = original_voice.voice
    if getattr(original_voice, 'local', False) and re.fullmatch(r'[A-Za-z0-9_-]+', voice):
        try:
            path = _recording_path(reason, language, voice, text)
            if path.stat().st_size <= 480_044:
                data = path.read_bytes()
                with wave.open(io.BytesIO(data)) as f:
                    frames = f.getnframes()
                    if ((f.getframerate(), f.getnchannels(), f.getsampwidth()) == (24000, 1, 2)
                            and 0 < frames <= 240000):
                        pcm = array.array('h', f.readframes(frames))
                        if len(pcm) == frames and max(map(abs, pcm)) > 64:
                            return text, RecordedReply(voice, text, data, frames / 24000)
        except (OSError, EOFError, wave.Error):
            pass
    return text, original_voice

def prepare_recorded_replies(language, voice):
    """Create missing clips outside playout and replace each complete WAV atomically."""
    if not getattr(voice, 'local', False) or not re.fullmatch(r'[A-Za-z0-9_-]+', voice.voice):
        return
    kinds = list(FIXED)
    if language == 'ja' and voice.voice == 'ja-JP-Aivis-Kanon-Normal':
        kinds.extend(CLARIFICATIONS)
    for kind in kinds:
        text, existing = fixed_reply(kind, language, voice)
        if isinstance(existing, RecordedReply):
            continue
        ASSETS.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ASSETS) as directory:
            path = Path(directory) / 'prepared.wav'
            voice.create(text, path)
            with wave.open(str(path)) as f:
                frames = f.getnframes()
                if (f.getframerate(), f.getnchannels(), f.getsampwidth()) != (24000, 1, 2) or not 0 < frames <= 240000:
                    raise ValueError('INVALID_LANGUAGE_REPLY')
                pcm = array.array('h', f.readframes(frames))
                if len(pcm) != frames or max(map(abs, pcm)) <= 64:
                    raise ValueError('SILENT_LANGUAGE_REPLY')
            path.replace(_recording_path(kind, language, voice.voice, text))
