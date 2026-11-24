"""Account binding for opt-in scheduled Twitch operations."""
import os
import re
from pathlib import Path

from ravora.settings import read_env


class TwitchError(RuntimeError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.retryable = retryable


def channel_login():
    login = os.environ.get('RAVORA_TWITCH_CHANNEL')
    if login is None:
        login = read_env(Path('.env')).get('RAVORA_TWITCH_CHANNEL', '')
    login = login.strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{1,25}', login):
        raise TwitchError('TWITCH_CHANNEL_LOGIN_REQUIRED')
    return login
