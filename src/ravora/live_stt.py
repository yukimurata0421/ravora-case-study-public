"""Submit one finalized host WAV to the regional Cloud Speech V2 recognizer."""
import base64
from contextlib import contextmanager
from http.client import HTTPException
import re
import time

from ravora.input_records import DeveloperError, InputRecords
from ravora.developer_http import RequestError
from ravora.providers import ProviderError, Transport
from ravora.settings import SettingsError
from ravora.stt import speech_access_token


class CloudHostSTT:
    provider = 'cloud_speech_v2'

    def __init__(self, settings, store, session):
        if not settings.cloud_project:
            raise SettingsError('GOOGLE_CLOUD_PROJECT_REQUIRED_FOR_STT')
        self.s = settings
        self.records = InputRecords(store, session, 'cloud')
        region = settings.voice_stt_region
        origin = f'https://{region}-speech.googleapis.com/'
        self.url = f'{origin}v2/projects/{settings.cloud_project}/locations/{region}/recognizers/_:recognize'
        self.http = Transport(settings.stt_timeout, enabled=settings.mode == 'cloud' and
                              settings.api_enabled and settings.stt_api_enabled)
        self.http.allowed_origins = (origin,)
        self.token = None
        self.refresh_at = 0

    def access_token(self):
        if time.monotonic() >= self.refresh_at:
            self.token = speech_access_token(self.s)
            self.refresh_at = time.monotonic() + 240
        return self.token

    @contextmanager
    def recognize(self, identifier, packet, language, cancel, source):
        locale = {'ja': 'ja-JP', 'en': 'en-US'}[language]
        try:
            key, fresh = self.records.create(identifier, 'audio', packet.data)
        except DeveloperError as exc:
            raise ProviderError(str(exc)) from None
        if not fresh:
            raise ProviderError(self.records.info(key)['code'] or 'HOST_INPUT_ALREADY_PROCESSED')
        model = self.s.voice_stt_model
        self.records.prepare_audio(key, packet, self.s, self.provider, model)
        diagnostics = {'input_source': 'live-host', 'actual_audio_seconds': packet.duration}
        start = time.monotonic()
        try:
            token = self.access_token()
            if cancel.is_set():
                raise ProviderError('HOST_STT_CANCELLED')
            if not self.records.dispatch(key, self.s):
                raise ProviderError(self.records.info(key)['code'] or 'HOST_INPUT_ALREADY_PROCESSED')
            result = self.http.json(self.url, {'Authorization': 'Bearer ' + token,
                                               'x-goog-user-project': self.s.quota_project or self.s.cloud_project},
                                    {'config': {'autoDecodingConfig': {}, 'model': model,
                                                'languageCodes': [locale],
                                                'features': {'enableAutomaticPunctuation': True}},
                                     'content': base64.b64encode(packet.data).decode('ascii')})
            if not isinstance(result, dict):
                raise ProviderError('INVALID_STT_RESULT')
            try:
                text = ' '.join(item['alternatives'][0]['transcript'].strip() for item in result.get('results', []))
                billed = result.get('metadata', {}).get('totalBilledDuration')
            except (KeyError, IndexError, TypeError, AttributeError):
                raise ProviderError('INVALID_STT_RESULT') from None
            if isinstance(billed, str) and re.fullmatch(r'[0-9]{1,5}(\.[0-9]{1,9})?s', billed):
                diagnostics['provider_billed_seconds'] = float(billed[:-1])
            if cancel.is_set():
                raise ProviderError('HOST_STT_CANCELLED')
            if not text.strip():
                raise ProviderError('STT_NO_FINAL_TRANSCRIPT')
            if len(text) > 1000 or any(ord(ch) < 32 and ch not in '\r\n\t' for ch in text):
                raise ProviderError('INVALID_STT_RESULT')
        except (OSError, HTTPException):
            self.records.state(key, 'outcome_unknown', 'PROVIDER_CONNECTION_FAILED')
            raise ProviderError('PROVIDER_CONNECTION_FAILED') from None
        except ProviderError as exc:
            code = str(exc)
            if code == 'PROVIDER_HTTP_401':
                self.refresh_at = 0
            if code not in ('STT_SESSION_LIMIT', 'STT_MONTHLY_AUDIO_LIMIT'):
                state = {'STT_NO_FINAL_TRANSCRIPT': 'empty', 'HOST_STT_CANCELLED': 'cancelled',
                         'PROVIDER_CONNECTION_FAILED': 'outcome_unknown'}.get(code, 'failed')
                self.records.state(key, state, code)
            raise
        finally:
            self.records.usage(key, {'response_model': model, 'local_diagnostics': diagnostics},
                               (time.monotonic() - start) * 1000)
        try:
            yield text
        except RequestError as exc:
            self.records.state(key, 'cancelled' if cancel.is_set() else 'failed', exc.code)
            raise
        else:
            self.records.state(key, 'accepted', language=locale, source=source)
