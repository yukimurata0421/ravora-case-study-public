"""Server-only provider settings. dotenv is data, never shell input."""
from dataclasses import dataclass, field
from pathlib import Path
import os
import re

from ravora.persona import DEFAULT_PERSONA, REPLY_STYLES, PersonaError, load_persona


def boolean(value):
    if value.lower() not in ('true','false'):raise ValueError()
    return value.lower()=='true'


class SettingsError(ValueError):
    pass


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise SettingsError('ENV_FILE_MUST_BE_PRIVATE_0600')
    result = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or key in result:
            raise SettingsError(f'INVALID_ENV_LINE_{number}')
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise SettingsError(f'INVALID_ENV_LINE_{number}')
            value = value[1:-1]
        else:
            value = value.split(' #', 1)[0].rstrip()
        if '\x00' in value or '\r' in value or '\n' in value:
            raise SettingsError(f'INVALID_ENV_LINE_{number}')
        result[key] = value
    return result


@dataclass(frozen=True)
class ProviderSettings:
    voice_profile: str = 'fast_streaming'
    voice_stt_region: str = 'asia-northeast1'
    voice_stt_model: str = 'long'
    voice_input_language: str = 'en-US'
    voice_tts_region: str = 'asia-northeast1'
    voice_tts_voice: str = 'en-US-Chirp3-HD-Aoede'
    voice_input_chunk_ms: int = 100
    voice_jitter_buffer_ms: int = 60
    early_reaction_enabled: bool = True
    early_reaction_stability: float = .8
    early_reaction_debounce_ms: int = 120
    early_reaction_cooldown_ms: int = 800
    early_reaction_max_requests: int = 2
    cloud_project: str = ''
    mode: str = 'offline'
    api_enabled: bool = False
    comment_source: str = 'fixture'
    comment_queue_size: int = 5
    comment_ttl: int = 45
    youtube_read_enabled: bool = False
    youtube_video_id: str = ''
    youtube_chat_id: str = field(default='', repr=False)
    youtube_poll_seconds: float = 5.
    youtube_max_requests: int = 40
    youtube_key: str = field(default='', repr=False)
    llm_provider: str = 'gemini'
    reaction_provider: str = 'jev'
    tts_provider: str = 'google_wavenet'
    gemini_model: str = 'gemini-3.5-flash-lite'
    persona: str = DEFAULT_PERSONA
    persona_file: str = field(default='', repr=False)
    reply_style: str = 'brief'
    jev_model: str = 'jev-latest'
    voice: str = 'en-US-Wavenet-F'
    language: str = 'en-US'
    speaking_rate: float = .96
    pitch: float = 0.
    timeout: float = 10.
    turn_timeout: float = 40.
    max_reply_chars: int = 1000
    sentence_chars: int = 240
    max_chunks: int = 8
    monthly_tts_chars: int = 1800000
    memory_turns: int = 4
    context_enabled: bool = True
    relationship_enabled: bool = True
    context_max_items: int = 8
    context_max_chars: int = 3600
    memory_enabled: bool = True
    memory_ttl_days: int = 30
    memory_queue_size: int = 16
    memory_wait_seconds: float = 10.
    memory_extractor: str = 'auto'
    memory_api_enabled: bool = False
    memory_model: str = ''
    memory_api_timeout: float = 8.
    memory_min_confidence: float = .7
    stt_api_enabled: bool = False
    stt_model: str = 'gemini-3.5-flash-lite'
    stt_language: str = 'auto'
    stt_timeout: float = 45.
    stt_max_seconds: int = 30
    stt_monthly_audio_seconds: int = 3600
    stt_session_requests: int = 60
    stt_min_rms: float = .001
    stt_provider: str = 'local_cpu'
    stt_model_path: str = 'assets/stt/local-model'
    stt_noise_percentile: float = 20.
    stt_margin_db: float = 6.
    stt_floor_db: float = -50.
    stt_min_speech_ms: int = 200
    stt_silence_ms: int = 300
    stt_pad_ms: int = 150
    stt_min_output_ms: int = 500
    stt_vad_probability: float = .5
    stt_no_speech: float = .6
    stt_log_probability: float = -1.
    stt_compression: float = 2.4
    stt_language_probability: float = .5
    developer_id: str = field(default='local-developer',repr=False)
    gemini_key: str = field(default='', repr=False)
    jev_key: str = field(default='', repr=False)
    tts_key: str = field(default='', repr=False)
    gcp_access_token: str = field(default='', repr=False)
    quota_project: str = field(default='', repr=False)

    def validate(self):
        if self.reply_style not in REPLY_STYLES:raise SettingsError('UNKNOWN_REPLY_STYLE')
        if (type(self.early_reaction_enabled) is not bool or type(self.early_reaction_stability) not in (int,float)
            or not .5<=self.early_reaction_stability<=1
            or type(self.early_reaction_debounce_ms) is not int or not 100<=self.early_reaction_debounce_ms<=1500
            or type(self.early_reaction_cooldown_ms) is not int or not 300<=self.early_reaction_cooldown_ms<=3000
            or type(self.early_reaction_max_requests) is not int or self.early_reaction_max_requests not in (1,2)):
            raise SettingsError('INVALID_EARLY_REACTION_SETTINGS')
        try:load_persona(self.persona,self.persona_file)
        except PersonaError as exc:raise SettingsError(str(exc)) from None
        if type(self.relationship_enabled) is not bool:raise SettingsError('INVALID_RELATIONSHIP_SETTING')
        if type(self.context_enabled) is not bool or type(self.context_max_items) is not int or not 3<=self.context_max_items<=12 or type(self.context_max_chars) is not int or not 1200<=self.context_max_chars<=8000:
            raise SettingsError('INVALID_CONVERSATION_CONTEXT')
        if self.voice_profile not in ('fast_streaming','legacy') or self.voice_stt_region not in ('asia-southeast1','asia-northeast1','us','eu') or self.voice_stt_model not in ('chirp_2','chirp_3','long'):
            raise SettingsError('INVALID_VOICE_PROFILE_OR_STT')
        if self.voice_input_language not in ('en-US','ja-JP') or self.voice_tts_region!='asia-northeast1' or not re.fullmatch(r'en-US-Chirp3-HD-[A-Za-z]{1,40}',self.voice_tts_voice):
            raise SettingsError('INVALID_STREAMING_VOICE')
        if type(self.voice_input_chunk_ms) is not int or not 20<=self.voice_input_chunk_ms<=200 or type(self.voice_jitter_buffer_ms) is not int or not 20<=self.voice_jitter_buffer_ms<=200:
            raise SettingsError('INVALID_STREAMING_BUFFER')
        if self.cloud_project and not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]',self.cloud_project):raise SettingsError('INVALID_CLOUD_PROJECT')
        if any(type(x) is not bool for x in (self.api_enabled,self.youtube_read_enabled,self.memory_enabled,self.memory_api_enabled,self.stt_api_enabled)):
            raise SettingsError('INVALID_API_SWITCH')
        if self.comment_source not in ('fixture','youtube-fixture','memory-fixture','youtube','developer'):
            raise SettingsError('INVALID_COMMENT_SOURCE')
        if not 1<=self.comment_queue_size<=20 or not 5<=self.comment_ttl<=180 or not 1<=self.youtube_poll_seconds<=60 or not 1<=self.youtube_max_requests<=100:
            raise SettingsError('INVALID_COMMENT_LIMITS')
        if self.youtube_video_id and not re.fullmatch(r'[A-Za-z0-9_-]{11}',self.youtube_video_id):
            raise SettingsError('INVALID_YOUTUBE_VIDEO_ID')
        if self.youtube_chat_id and (len(self.youtube_chat_id)>1024 or any(ord(c)<33 or ord(c)>126 for c in self.youtube_chat_id)):
            raise SettingsError('INVALID_YOUTUBE_CHAT_ID')
        if self.mode not in ('offline', 'cloud'):
            raise SettingsError('INVALID_PROVIDER_MODE')
        if (self.llm_provider, self.reaction_provider, self.tts_provider) != ('gemini','jev','google_wavenet'):
            raise SettingsError('UNSUPPORTED_PROVIDER_COMBINATION')
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,100}', self.gemini_model) or not re.fullmatch(r'[a-zA-Z0-9._-]{1,100}', self.jev_model):
            raise SettingsError('INVALID_MODEL_NAME')
        if not re.fullmatch(r'en-(?:US|GB|AU|IN)-Wavenet-[A-Z]', self.voice) or self.language != self.voice[:5]:
            raise SettingsError('ENGLISH_WAVENET_VOICE_REQUIRED')
        if not (.5 <= self.speaking_rate <= 1.5 and -6 <= self.pitch <= 6 and 1 <= self.timeout <= 20 and 5 <= self.turn_timeout <= 90):
            raise SettingsError('INVALID_VOICE_OR_TIMEOUT')
        if not (80 <= self.max_reply_chars <= 2000 and 60 <= self.sentence_chars <= 400 and 1 <= self.max_chunks <= 12 and 0 <= self.memory_turns <= 8 and 1 <= self.monthly_tts_chars <= 4000000):
            raise SettingsError('INVALID_CONVERSATION_LIMIT')
        if type(self.memory_ttl_days) is not int or type(self.memory_queue_size) is not int or not (1<=self.memory_ttl_days<=90 and 1<=self.memory_queue_size<=32 and .05<=self.memory_wait_seconds<=25 and 1<=self.memory_api_timeout<=20 and 0<=self.memory_min_confidence<=1):
            raise SettingsError('INVALID_MEMORY_LIMITS')
        if self.memory_extractor not in ('auto','local_rules','gemini') or (self.memory_model and not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,100}',self.memory_model)):
            raise SettingsError('INVALID_MEMORY_EXTRACTOR')
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,100}',self.stt_model) or self.stt_language not in ('auto','en','ja'):
            raise SettingsError('INVALID_STT_MODEL_OR_LANGUAGE')
        if any(type(v) is not int for v in (self.stt_max_seconds,self.stt_monthly_audio_seconds,self.stt_session_requests)) or not (1<=self.stt_timeout<=90 and 1<=self.stt_max_seconds<=30 and 60<=self.stt_monthly_audio_seconds<=86400 and 1<=self.stt_session_requests<=240 and 0<=self.stt_min_rms<=.05):
            raise SettingsError('INVALID_STT_LIMITS')
        if self.stt_provider!='local_cpu' or not re.fullmatch(r'assets/stt/[A-Za-z0-9_./-]{1,180}',self.stt_model_path) or '..' in Path(self.stt_model_path).parts:
            raise SettingsError('INVALID_LOCAL_STT_PROVIDER_OR_PATH')
        if not (.3<=self.stt_vad_probability<=.8 and .2<=self.stt_no_speech<=.9 and -2<=self.stt_log_probability<=-.1 and 1.5<=self.stt_compression<=3.5 and 0<=self.stt_language_probability<=1):raise SettingsError('INVALID_LOCAL_STT_THRESHOLDS')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',self.developer_id):raise SettingsError('INVALID_DEVELOPER_ID')
        if any(any(c in value for c in '\r\n\0') for value in (self.gemini_key,self.jev_key,self.tts_key,self.gcp_access_token,self.quota_project,self.youtube_key)):
            raise SettingsError('INVALID_CREDENTIAL_FORMAT')
        return self


    def missing(self):
        return [name for name, present in [('GEMINI_API_KEY',self.gemini_key),('JEV_API_KEY',self.jev_key),('GOOGLE_TTS_API_KEY or GOOGLE_CLOUD_ACCESS_TOKEN',self.tts_key or self.gcp_access_token)] if not present]

    def require_ready(self):
        self.validate()
        if self.mode == 'cloud' and self.api_enabled and self.missing():
            raise SettingsError('MISSING_SETTINGS: '+', '.join(self.missing()))

    def public(self):
        names=('mode','api_enabled','comment_source','comment_queue_size','comment_ttl','youtube_read_enabled','youtube_poll_seconds','youtube_max_requests','llm_provider','reaction_provider','tts_provider','gemini_model','jev_model','voice','language','speaking_rate','pitch','timeout','turn_timeout','max_reply_chars','sentence_chars','max_chunks','monthly_tts_chars','memory_turns','memory_enabled','memory_ttl_days','memory_queue_size','memory_wait_seconds','memory_extractor','memory_api_enabled','memory_model','memory_api_timeout','memory_min_confidence','stt_api_enabled','stt_model','stt_language','stt_timeout','stt_max_seconds','stt_monthly_audio_seconds','stt_session_requests','stt_min_rms')
        return {name:getattr(self,name) for name in (*names,'persona','reply_style','early_reaction_enabled','early_reaction_stability',
            'early_reaction_debounce_ms','early_reaction_cooldown_ms','early_reaction_max_requests',
            'context_enabled','context_max_items','context_max_chars','relationship_enabled','voice_profile','voice_stt_region','voice_stt_model','voice_input_language',
            'voice_tts_region','voice_tts_voice','voice_input_chunk_ms','voice_jitter_buffer_ms','stt_provider','stt_model_path','stt_noise_percentile',
            'stt_margin_db','stt_floor_db','stt_min_speech_ms','stt_silence_ms','stt_pad_ms','stt_min_output_ms',
            'stt_vad_probability','stt_no_speech','stt_log_probability','stt_compression','stt_language_probability')}


def load_settings(path: Path, *, mode=None, environ=None):
    values = read_env(path)
    values.update(os.environ if environ is None else environ)
    spec = {'api_enabled':('RAVORA_API_ENABLED',boolean),'comment_source':('RAVORA_COMMENT_SOURCE',str),'comment_queue_size':('RAVORA_COMMENT_QUEUE_SIZE',int),'comment_ttl':('RAVORA_COMMENT_TTL_SECONDS',int),'youtube_read_enabled':('YOUTUBE_READ_ENABLED',boolean),'youtube_video_id':('YOUTUBE_VIDEO_ID',str),'youtube_chat_id':('YOUTUBE_LIVE_CHAT_ID',str),'youtube_poll_seconds':('YOUTUBE_MIN_POLL_SECONDS',float),'youtube_max_requests':('YOUTUBE_MAX_REQUESTS',int),'youtube_key':('YOUTUBE_API_KEY',str),'mode':('RAVORA_MODE',str),'llm_provider':('RAVORA_LLM_PROVIDER',str),'reaction_provider':('RAVORA_REACTION_PROVIDER',str),'tts_provider':('RAVORA_TTS_PROVIDER',str),'gemini_model':('GEMINI_MODEL',str),'jev_model':('JEV_MODEL',str),'voice':('GOOGLE_TTS_VOICE',str),'language':('GOOGLE_TTS_LANGUAGE',str),'speaking_rate':('GOOGLE_TTS_SPEAKING_RATE',float),'pitch':('GOOGLE_TTS_PITCH',float),'timeout':('RAVORA_API_TIMEOUT_SECONDS',float),'turn_timeout':('RAVORA_TURN_TIMEOUT_SECONDS',float),'max_reply_chars':('RAVORA_MAX_REPLY_CHARS',int),'sentence_chars':('RAVORA_SENTENCE_CHARS',int),'max_chunks':('RAVORA_MAX_SPEECH_CHUNKS',int),'monthly_tts_chars':('RAVORA_MONTHLY_TTS_CHAR_LIMIT',int),'memory_turns':('RAVORA_MEMORY_TURNS',int),'gemini_key':('GEMINI_API_KEY',str),'jev_key':('JEV_API_KEY',str),'tts_key':('GOOGLE_TTS_API_KEY',str),'gcp_access_token':('GOOGLE_CLOUD_ACCESS_TOKEN',str),'quota_project':('GOOGLE_CLOUD_QUOTA_PROJECT',str)}
    kwargs={}
    spec.update({'context_enabled':('RAVORA_CONTEXT_ENABLED',boolean),'context_max_items':('RAVORA_CONTEXT_MAX_ITEMS',int),
        'relationship_enabled':('RAVORA_RELATIONSHIP_ENABLED',boolean),
        'early_reaction_enabled':('RAVORA_EARLY_REACTION_ENABLED',boolean),
        'early_reaction_stability':('RAVORA_EARLY_REACTION_STABILITY',float),
        'early_reaction_debounce_ms':('RAVORA_EARLY_REACTION_DEBOUNCE_MS',int),
        'early_reaction_cooldown_ms':('RAVORA_EARLY_REACTION_COOLDOWN_MS',int),
        'early_reaction_max_requests':('RAVORA_EARLY_REACTION_MAX_REQUESTS',int),
        'persona':('RAVORA_PERSONA',str),'persona_file':('RAVORA_PERSONA_FILE',str),
        'reply_style':('RAVORA_REPLY_STYLE',str),
        'context_max_chars':('RAVORA_CONTEXT_MAX_CHARS',int),
        'voice_profile':('RAVORA_VOICE_PROFILE',str),'voice_stt_region':('RAVORA_VOICE_STT_REGION',str),
        'voice_stt_model':('RAVORA_VOICE_STT_MODEL',str),'voice_input_language':('RAVORA_VOICE_INPUT_LANGUAGE',str),
        'voice_tts_region':('RAVORA_VOICE_TTS_REGION',str),'voice_tts_voice':('RAVORA_VOICE_TTS_VOICE',str),
        'voice_input_chunk_ms':('RAVORA_VOICE_INPUT_CHUNK_MS',int),'voice_jitter_buffer_ms':('RAVORA_VOICE_JITTER_BUFFER_MS',int),
        'cloud_project':('GOOGLE_CLOUD_PROJECT',str)})
    spec.update({'memory_enabled':('RAVORA_MEMORY_ENABLED',boolean),'memory_ttl_days':('RAVORA_MEMORY_TTL_DAYS',int),
                 'memory_queue_size':('RAVORA_MEMORY_QUEUE_SIZE',int),'memory_wait_seconds':('RAVORA_MEMORY_WAIT_SECONDS',float),
                 'memory_extractor':('RAVORA_MEMORY_EXTRACTOR',str),'memory_api_enabled':('RAVORA_MEMORY_API_ENABLED',boolean),'memory_model':('GEMINI_MEMORY_MODEL',str),
                 'memory_api_timeout':('RAVORA_MEMORY_API_TIMEOUT_SECONDS',float),'memory_min_confidence':('RAVORA_MEMORY_MIN_CONFIDENCE',float)})
    spec.update({'stt_api_enabled':('RAVORA_STT_API_ENABLED',boolean),'stt_model':('GEMINI_STT_MODEL',str),
        'stt_language':('RAVORA_STT_LANGUAGE',str),'stt_timeout':('RAVORA_STT_TIMEOUT_SECONDS',float),
        'stt_max_seconds':('RAVORA_STT_MAX_SECONDS',int),'stt_monthly_audio_seconds':('RAVORA_STT_MONTHLY_AUDIO_SECONDS',int),
        'stt_session_requests':('RAVORA_STT_SESSION_REQUESTS',int),'stt_min_rms':('RAVORA_STT_MIN_RMS',float),
        'developer_id':('RAVORA_DEVELOPER_ID',str)})
    spec.update({'stt_provider':('RAVORA_STT_PROVIDER',str),'stt_model_path':('RAVORA_STT_MODEL_PATH',str),
        'stt_noise_percentile':('RAVORA_VAD_NOISE_PERCENTILE',float),'stt_margin_db':('RAVORA_VAD_MARGIN_DB',float),
        'stt_floor_db':('RAVORA_VAD_FLOOR_DBFS',float),'stt_min_speech_ms':('RAVORA_VAD_MIN_SPEECH_MS',int),
        'stt_silence_ms':('RAVORA_VAD_MIN_SILENCE_MS',int),'stt_pad_ms':('RAVORA_VAD_PAD_MS',int),
        'stt_min_output_ms':('RAVORA_VAD_MIN_OUTPUT_MS',int),'stt_vad_probability':('RAVORA_STT_VAD_PROBABILITY',float),
        'stt_no_speech':('RAVORA_STT_NO_SPEECH_THRESHOLD',float),'stt_log_probability':('RAVORA_STT_LOG_PROB_THRESHOLD',float),
        'stt_compression':('RAVORA_STT_COMPRESSION_THRESHOLD',float),'stt_language_probability':('RAVORA_STT_LANGUAGE_PROBABILITY',float)})
    for field_name,(env_name,cast) in spec.items():
        if env_name in values:
            try:kwargs[field_name]=cast(values[env_name])
            except ValueError:raise SettingsError('INVALID_SETTING: '+env_name) from None
    if mode:kwargs['mode']=mode
    if kwargs.get('persona_file'):
        selected=Path(kwargs['persona_file'])
        if not selected.is_absolute():selected=path.resolve().parent/selected
        kwargs['persona_file']=str(selected)
    return ProviderSettings(**kwargs).validate()
