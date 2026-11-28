"""Keep a bounded set of local language hints for recently received viewer comments."""
from hashlib import sha256
from threading import Lock

from ravora.spoken_language import clear_reply_language

_hints = {}
_lock = Lock()


def remember_language(text):
    key = sha256(text.encode()).digest()
    with _lock:
        if key in _hints:
            return
    language = clear_reply_language(text)
    with _lock:
        _hints[key] = language
        if len(_hints) > 64:
            del _hints[next(iter(_hints))]


def reply_language(text):
    key = sha256(text.encode()).digest()
    with _lock:
        if key in _hints:
            return _hints[key]
    return clear_reply_language(text)
