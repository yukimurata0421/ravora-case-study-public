"""Schedule public host and viewer turns on the headless program timeline."""
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
import re
import threading
import time
import unicodedata

import numpy as np

from ravora.comments import Comment, VIEWER_CHAT_SOURCES, clean
from ravora.developer_http import RequestError
from ravora.edge_grpc import pcm_wave
from ravora.live_media import Clip, FPS, RATE, SILENCE, SpeechPrefix, Stage
from ravora.live_mic import LiveMicrophone
from ravora.live_reverb import DEFAULT_RAY_REVERB_MIX, RayReverb
from ravora.live_stt import CloudHostSTT
from ravora.live_handsfree import LiveHandsfree
from ravora.live_overlay import ChatLine, caption_layout
from ravora.local_stt import LocalSTT
from ravora.providers import ProviderError
from ravora.ray_character import EXPRESSIONS, MOTIONS
from ravora.stt import AudioPacket


_CHAT_DECORATIONS = re.compile(r'https?://\S+|www\.\S+|(?<!\w)@[\w.-]+|:[\w+-]+:', re.IGNORECASE)
_QUOTATION = re.compile(r'「[^」]*」|『[^』]*』|“[^”]*”|"[^"]*"')
_JAPANESE = re.compile(r'[ぁ-ゖァ-ヺ々〆〇\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]')
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_LATIN_PHRASE = re.compile(r'[A-Za-z]\s+[A-Za-z]')


def youtube_reply_language(comment, fallback):
    """Choose between the two live voices using the commenter's own words."""
    text = _CHAT_DECORATIONS.sub(' ', unicodedata.normalize('NFKC', comment))
    surrounding = _QUOTATION.sub(' ', text)
    if surrounding != text and (_JAPANESE.search(surrounding) or len(_LATIN_WORD.findall(surrounding)) >= 2):
        text = surrounding
    japanese = len(_JAPANESE.findall(text))
    words = _LATIN_WORD.findall(text)
    if not japanese:
        return 'en' if words else fallback
    # Count Latin words rather than letters so a product name does not outweigh Japanese prose.
    if len(words) >= japanese and _LATIN_PHRASE.search(text):
        return 'en'
    return 'ja'


@dataclass(frozen=True)
class MonitorClip:
    sequence: int
    start_frame: int
    end_frame: int
    viewer_turn: str | None
    wav: bytes


class LiveProgram:
    def __init__(self, pipeline, audio_directory, *, host_language=None, reply_profiles=None,
                 onset_dbfs=None, silence_ms=800, host_trim_db=0.0, host_mic_cleanup=True,
                 ray_reverb_mix=DEFAULT_RAY_REVERB_MIX, bgm=None):
        self.pipeline = pipeline
        self.bridge = pipeline.bridge
        self.audio_directory = audio_directory
        self.reverb = RayReverb(ray_reverb_mix)
        self.bgm = bgm
        language = host_language or pipeline.s.stt_language
        self.stt = LocalSTT(pipeline.s)
        self.cloud_stt = (CloudHostSTT(pipeline.s, pipeline.inbox.store, pipeline.inbox.session)
                          if pipeline.s.mode == 'cloud' and pipeline.s.api_enabled and pipeline.s.stt_api_enabled else None)
        self.reply_profiles = reply_profiles or {}
        self.host_stt_counts = Counter()
        self.ingress = threading.Lock()
        self.stopped = threading.Event()
        self.transport = None
        self.pending_host = None
        self.handsfree = LiveHandsfree(self.submit_audio, pipeline.s.stt_max_seconds,
                                       pipeline.s.handsfree_onset_dbfs if onset_dbfs is None else onset_dbfs,
                                       silence_ms, language=language)
        self.mic = LiveMicrophone(handsfree=self.handsfree, host_trim_db=host_trim_db,
                                  host_mic_cleanup=host_mic_cleanup)
        self.host_caption = ''
        self.host_caption_queue = deque()
        self.host_caption_until = 0
        self.finished_audio_turn = None
        self.lock = threading.RLock()
        self.bridge.controller.program_pcm = self._receive_pcm
        self.prefix = None
        self.monitor_origin = 0
        self.host_recognition = None
        self.stt_final = None
        self.latency = {}
        self.last_latency = None
        self.ray_signal = False
        self.stage = Stage()
        self.chat_status = {'status': {'youtube': 'waiting_for_live', 'twitch': 'connecting',
                                       'developer': 'host_only'}.get(pipeline.s.comment_source, 'fixture')}
        if pipeline.s.comment_source == 'twitch':
            self.chat_status['transport'] = 'twitch_eventsub_websocket'
        self.chat_key = None
        self.chat = ()
        self.chat_ids = ()
        self.selected = self.history_receipt = None
        self.previous = self.last_spoken = ''
        self.previous_font = self.last_spoken_font = Stage().caption_font
        self.history_until = 0
        self.monitor = None
        self.monitor_reset = 0
        self.monitor_sequence = 0
        self.monitor_history = deque(maxlen=24)
        self.monitor_removal_revision = pipeline.inbox.removal_revision
        self.frame = 0
        self.first_program_frame = True
        self.completed = 0
        self.failed = 0
        self.last_error = None
        self.next_check = 0
        self.initiative_waiting = False
        self.initiative_revision = 0
        self.token = self.clip = None
        self.audio = deque()
        self.actions = []
        self.cursor = 0
        self.reply_failed = False
        self.generated = False
        self.reaction = 'neutral'
        self.action = ('neutral', 'none')

    @property
    def host_stt_provider(self):
        if self.cloud_stt and self.handsfree.language != 'auto':
            return self.cloud_stt.provider
        return self.stt.provider

    @property
    def conversation_language(self):
        if not self.reply_profiles:
            return None
        language = self.handsfree.language
        return self.pipeline.s.language[:2] if language == 'auto' else language

    def select_language(self, language):
        if language not in ('ja', 'en'):
            raise RequestError('INVALID_CONVERSATION_LANGUAGE')
        if not self.reply_profiles:
            raise RequestError('OFFLINE_FIXTURE_LANGUAGE', 409)
        with self.lock, self.handsfree.condition:
            self.handsfree.language = language
            return {'conversation_language': language, 'host_language': language}

    def submit_audio(self, identifier, data, *, already_aired=False, cancel=None, language=None):
        if not re.fullmatch(r'[a-f0-9]{32}', identifier or ''):
            raise RequestError('INVALID_INPUT_ID')
        if not self.ingress.acquire(blocking=False):
            raise RequestError('HOST_MIC_BUSY', 409)
        try:
            with self.lock:
                if language is None:
                    language = self.handsfree.language
                if language not in ('ja', 'en', 'auto'):
                    raise RequestError('INVALID_CONVERSATION_LANGUAGE')
            inbox = self.pipeline.inbox
            comment = Comment('live-host', inbox.session, identifier, 'host', 'Host', '', time.time())
            with inbox.store.connect() as db:
                prior = db.execute('SELECT status FROM comment_inbox WHERE receipt=?', (comment.receipt,)).fetchone()
            if prior:
                return {'status': prior[0]}
            try:
                packet = AudioPacket.parse(data, maximum_seconds=self.pipeline.s.stt_max_seconds,
                                           minimum_rms=self.pipeline.s.stt_min_rms)
                cancel = cancel or self.stopped
                started = time.monotonic()
                if self.cloud_stt and language != 'auto':
                    with self.cloud_stt.recognize(identifier, packet, language, cancel, comment.receipt) as text:
                        return self._queue_host(identifier, text, data, already_aired, cancel, language, started)
                result = self.stt.transcribe(packet, cancel=cancel, language=language)
            except ProviderError as exc:
                with self.lock:
                    self.host_stt_counts[str(exc)] += 1
                raise RequestError(str(exc), 422) from None
            if not result['speech']:
                diag = self.stt.usage.get('local_diagnostics', {})
                if diag.get('reason') == 'RMS_VAD_NO_SPEECH' or diag.get('duration_after_vad') == 0:
                    reason = 'HOST_NO_VOICE'
                elif diag.get('language_probability', 1) < self.stt.s.stt_language_probability:
                    reason = 'HOST_LANGUAGE_UNCERTAIN'
                else:
                    reason = 'HOST_SPEECH_UNCLEAR'
                with self.lock:
                    self.host_stt_counts[reason] += 1
                raise RequestError(reason, 422)
            return self._queue_host(identifier, result['text'], data, already_aired, cancel, language, started)
        finally:
            self.ingress.release()

    def _queue_host(self, identifier, text, data, already_aired, cancel, language, started):
        stt_final = time.monotonic()
        inbox = self.pipeline.inbox
        comment = Comment('live-host', inbox.session, identifier, 'host', 'Host',
                          clean(text, 1000), time.time())
        clip = None if already_aired else Clip('Host', comment.text, data)
        host_pages = caption_layout(comment.text, host_transcript=True)[1] if already_aired else ()
        with self.lock:
            if self.stopped.is_set() or (cancel and cancel.is_set()):
                raise RequestError('PROGRAM_STOPPED', 409)
            if self.pipeline.paused_reason:
                raise RequestError('CONVERSATION_PAUSED', 409)
            status = inbox.put(comment)
            if status != 'queued':
                raise RequestError('HOST_INPUT_' + status.upper(), 409)
            self.host_stt_counts['accepted'] += 1
            # The inbox admits only one queued turn per host.
            reply_language = self.pipeline.s.language[:2] if language == 'auto' else language
            self.host_recognition = (comment.receipt, reply_language, stt_final, round(1000*(stt_final-started), 2))
            self.next_check = 0
            if already_aired:
                self.host_caption = host_pages[0]
                self.host_caption_queue = deque(host_pages[1:])
                self.host_caption_until = self.frame + 6 * FPS
            else:
                self.pending_host = (comment.receipt, clip, time.monotonic() + self.pipeline.s.comment_ttl)
        return {'status': status}

    def _begin(self):
        if self.pending_host and time.monotonic() >= self.pending_host[2]:
            self.pending_host = None
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + .12
        if self.finished_audio_turn and not self.bridge.worker.is_alive():
            for path in self.audio_directory.glob(self.finished_audio_turn + '-*'):
                path.unlink(missing_ok=True)
            self.finished_audio_turn = None
        self.initiative_revision = (self.pipeline.inbox.display_revision,self.host_recognition)
        quiet = self._quiet_for_followup()
        if not quiet and self.bridge.controller.character:
            self.bridge.controller.character.note_mic_activity()
        value = self.pipeline.next(allow_host_input=True, allow_initiative=quiet)
        if not value['available']:
            return
        comment = value['comment']
        receipt = comment['receipt']
        viewer_comment = comment['source'] in VIEWER_CHAT_SOURCES
        language = self.conversation_language
        self.latency = {'turn_to_program_signal_ms': None, 'prefix_frames': 0, 'prefix_wait_frames': 0}
        self.stt_final = None
        if self.host_recognition and self.host_recognition[0] == receipt:
            _, language, self.stt_final, self.latency['stt_ms'] = self.host_recognition
            self.latency['stt_final_to_program_signal_ms'] = None
            self.host_recognition = None
        if self.reply_profiles:
            # next() admits a turn only after the preceding worker and playout finish.
            if viewer_comment:
                language = youtube_reply_language(comment['comment'], language)
            elif comment['source'] == 'ray-initiative':
                language = 'en'
            controller = self.bridge.controller
            controller.s, controller.text, controller.speech = self.reply_profiles[language]
        self.turn_started = time.monotonic()
        try:
            self.token = self.pipeline.begin(receipt)
        except ValueError as exc:
            if str(exc) != 'COMMENT_NO_LONGER_AVAILABLE':
                raise
            self.pipeline.inbox.finish(receipt, 'expired')
            return
        self.initiative_waiting = comment['source'] == 'ray-initiative'
        self.last_error = None
        self.selected = receipt if viewer_comment else None
        self.last_spoken = ''
        if self.pending_host and self.pending_host[0] == receipt:
            self.clip = self.pending_host[1]
            self.pending_host = None
        self.cursor = 0
        self.reply_failed = False
        self.generated = False
        self.actions.clear()
        self.audio.clear()
        self.reaction = 'neutral'
        self.action = ('neutral', 'none')

    def _quiet_for_followup(self):
        return not (self.ingress.locked() or self.pending_host) and self.mic.allows_initiative()

    def _receive_pcm(self, turn_id, text, packet):
        with self.lock:
            if (self.stopped.is_set() or not self.token or self.token['turn_id'] != turn_id
                    or not self.bridge.fence.current(self.token)):
                return
            if self.prefix is None:
                self.prefix = SpeechPrefix(text)
            self.prefix.feed(packet)

    def _end(self, played=False):
        if not played:
            self.monitor_reset += 1
            self.monitor_history.clear()
        self.previous = self.last_spoken if played else ''
        self.previous_font = self.last_spoken_font
        self.history_until = self.frame + 6 * FPS if played else 0
        self.history_receipt = self.selected if played else None
        self.selected = None
        self.last_spoken = ''
        self.initiative_waiting = False
        self.finished_audio_turn = self.token['turn_id']
        self.last_latency = {**self.latency, 'played': played}
        self.clip = self.token = self.monitor = self.prefix = None
        self.reverb.reset()
        self.monitor_origin = 0
        self.audio.clear()

    def _events(self):
        for event in self.bridge.poll(self.cursor)['events']:
            self.cursor += 1
            kind = event['kind']
            if kind == 'reaction' and event.get('reaction') in EXPRESSIONS:
                self.reaction = event['reaction']
            elif kind == 'character_action':
                face, motion = event.get('expression'), event.get('motion')
                if face in EXPRESSIONS and motion in MOTIONS:
                    self.actions.append((face, motion))
            elif kind == 'audio':
                self.audio.append(event)
            elif kind == 'complete':
                self.generated = True
            elif kind in ('error', 'held', 'cancelled'):
                self.failed += 1
                self.last_error = event.get('code')
                if kind != 'cancelled' and self.clip and self.clip.speaker == 'Host':
                    self.reply_failed = True
                    self.audio.clear()
                else:
                    self._end()
                break

    def next_frame(self):
        with self.lock:
            if self.first_program_frame:
                self.first_program_frame = False
                character = getattr(self.bridge.controller,'character',None)
                if character:
                    character.start_opening_window()
            stage, pcm = self._turn_frame()
            # Measure Ray before mixing the host. Count the frame only after writer.write succeeds.
            self.ray_signal = (stage.speaker == 'Ray' and self.latency.get('turn_to_program_signal_ms') is None
                               and bool(np.any(np.abs(np.frombuffer(pcm,'<i2').astype(np.int32)) >= 33)))
            inbox = self.pipeline.inbox
            key = (self.selected, inbox.display_revision)
            if key != self.chat_key:
                rows = inbox.display_comments(self.selected)
                self.chat_ids = tuple(row[0] for row in rows)
                self.chat = tuple(ChatLine(row[1], row[2]) for row in rows)
                self.chat_key = key
                if self.history_receipt and self.history_receipt not in self.chat_ids:
                    self.previous = ''
            selected = self.chat_ids.index(self.selected) if self.selected in self.chat_ids else -1
            if stage.speaker == 'Ray' and stage.caption != self.last_spoken:
                if self.last_spoken:
                    self.previous = self.last_spoken
                    self.previous_font = self.last_spoken_font
                    self.history_until = self.frame + 6 * FPS
                    self.history_receipt = self.selected
                self.last_spoken = stage.caption
                self.last_spoken_font = stage.caption_font
            if self.frame >= self.history_until:
                self.previous = ''
            if self.host_caption and self.frame >= self.host_caption_until:
                self.host_caption = self.host_caption_queue.popleft() if self.host_caption_queue else ''
                self.host_caption_until = self.frame + 6 * FPS
            mixed = self.mic.mix(pcm, ray_speaking=stage.speaker == 'Ray', host_playback=stage.speaker == 'Host')
            previous = self.previous
            caption_font = stage.caption_font
            if not stage.speaker and self.last_spoken:
                previous = self.last_spoken
                caption_font = self.last_spoken_font
            elif not stage.speaker and not self.token and previous:
                caption_font = self.previous_font
            return replace(stage, chat=self.chat, selected=selected, previous=previous, caption_font=caption_font,
                           host_caption=self.host_caption, replying=bool(self.last_spoken),
                           thinking=bool(self.token and not stage.speaker and not self.last_spoken)), mixed

    def _prepare_ray_audio(self, name, *, final):
        start = self.clip.position
        pcm = self.reverb.render(self.clip.pcm[start:], final=final)
        self.clip.playout_pcm = self.clip.pcm[:start] + pcm
        if not pcm:
            self.monitor = None
            return
        self.monitor = (name, pcm_wave(pcm, RATE))
        self.monitor_origin = start
        self.monitor_sequence += 1
        frames = (len(pcm) + len(SILENCE) - 1) // len(SILENCE)
        self.monitor_history.append(MonitorClip(self.monitor_sequence, self.frame, self.frame + frames,
                                                self.token['turn_id'] if self.selected else None, self.monitor[1]))

    def monitor_packet(self, after):
        """A new listener joins live. An existing listener drains unexpired clips in order."""
        with self.lock:
            inbox = self.pipeline.inbox
            revision = inbox.removal_revision
            if revision != self.monitor_removal_revision:
                viewer_turns = {c.viewer_turn for c in self.monitor_history if c.viewer_turn}
                removed = {turn for turn in viewer_turns if inbox.removed_turn(turn)}
                if removed:
                    self.monitor_history = deque((c for c in self.monitor_history if c.viewer_turn not in removed),
                                                 maxlen=self.monitor_history.maxlen)
                    self.monitor_reset += 1
                self.monitor_removal_revision = revision
            latest = self.monitor_sequence
            generation = self.monitor_reset
            if self.stopped.is_set() or (self.token and not self.bridge.fence.current(self.token)):
                return latest, generation + 1, self.frame, 0, True, b''
            if after == latest:
                return latest, generation, self.frame, 0, False, b''
            if after is not None:
                for clip in self.monitor_history:
                    if clip.sequence == after + 1:
                        return clip.sequence, generation, clip.start_frame, 0, False, clip.wav
            if self.monitor and self.monitor_history and self.monitor_history[-1].sequence == latest:
                clip = self.monitor_history[-1]
                offset = (self.clip.position - self.monitor_origin) // 2
                return latest, generation, clip.start_frame, offset, True, clip.wav
            return latest, generation, self.frame, 0, True, b''

    def _turn_frame(self):
        # This clock tracks local program playout, including during RTMPS recovery.
        if self.token is None:
            self._begin()
        if self.token is None:
            return Stage(), SILENCE
        if self.initiative_waiting:
            inbox = self.pipeline.inbox
            revision = (inbox.display_revision,self.host_recognition)
            changed = self.initiative_revision != revision
            self.initiative_revision = revision
            if not self._quiet_for_followup() or (changed and inbox.human_waiting()):
                self.pipeline.cancel()
                self._end()
                self.next_check = 0
                return Stage(), SILENCE
        if not self.bridge.fence.current(self.token):
            self._end()
            return Stage(), SILENCE
        self._events()
        if self.token is None:
            return Stage(), SILENCE
        if self.clip and self.clip.done:
            self.clip = self.monitor = None
        if self.reply_failed and self.clip is None:
            self._end()
            return Stage(), SILENCE
        if self.clip is None and self.audio:
            if self.handsfree.speaking():
                return Stage(expression=self.reaction, motion='none'), SILENCE
            event = self.audio.popleft()
            index, text = event['index'], event['text']
            if self.bridge.claim(self.token, index):
                name = f"{self.token['turn_id']}-{index}.wav"
                data = (self.audio_directory / name).read_bytes()
                prefix = self.prefix if index == 0 else None
                self.clip = Clip('Ray', text, data, mouth_plan=event.get('mouth_plan',()),
                                 word_starts=event.get('word_starts',()),
                                 layout=prefix.layout if prefix and prefix.text == text else None)
                if prefix:
                    self.clip.position = prefix.consumed
                    self.prefix = None
                self._prepare_ray_audio(name, final=True)
                if index < len(self.actions):
                    self.action = self.actions[index]
        elif self.clip is None and self.prefix and not self.handsfree.speaking():
            self.clip = self.prefix.take()
            if self.clip:
                name = f"{self.token['turn_id']}-0-p{self.prefix.consumed // len(SILENCE)}.wav"
                self._prepare_ray_audio(name, final=False)
                if self.actions:
                    self.action = self.actions[0]
            elif self.prefix.consumed:
                self.latency['prefix_wait_frames'] += 1
        face, motion = self.action
        expression = face if face != 'neutral' else self.reaction
        if self.clip and not self.clip.done:
            if self.initiative_waiting and not self.pipeline.followup_available():
                self.pipeline.cancel()
                self._end()
                return Stage(), SILENCE
            self.initiative_waiting = False
            caption, pcm = self.clip.take()
            return Stage(self.clip.speaker, caption, expression, motion,
                         highlight=self.clip.highlight, mouth=self.clip.mouth, caption_font=self.clip.caption_font), pcm
        if self.generated and not self.bridge.worker.is_alive():
            played = self.pipeline.played(self.token, program_latency=self.latency)
            if played:
                self.completed += 1
            else:
                self.pipeline.cancel()
                self.failed += 1
                self.last_error = 'PLAYOUT_ACK_REJECTED'
            self._end(played=played)
            return Stage(), SILENCE
        return Stage(expression=expression, motion=motion), SILENCE

    def status(self):
        with self.lock:
            reader = self.pipeline.reader
            return {'transport': self.transport.status() if self.transport else None,
                    'stage': asdict(self.stage), 'frame': self.frame, 'turns_played': self.completed,
                    'turns_failed': self.failed, 'last_error': self.last_error,
                    'conversation_paused': self.pipeline.paused_reason,
                    'chat': reader.snapshot() if reader else self.chat_status,
                    'reply_mode': self.pipeline.s.mode, 'monitor_reset': self.monitor_reset,
                    'monitor_protocol': 'sequence_v1',
                    'monitor_audio': self.monitor[0] if self.monitor else None,
                    'monitor_offset_seconds': (self.clip.position-self.monitor_origin) / (2 * RATE) if self.monitor else 0,
                    'last_program_latency': self.last_latency,
                    'handsfree': self.handsfree.status(), 'host_language': self.handsfree.language,
                    'conversation_language': self.conversation_language,
                    'host_stt_provider': self.host_stt_provider, 'host_trim_db': self.mic.host_trim_db,
                    'bgm': self.bgm.status() if self.bgm is not None else None,
                    'host_stt_counts': dict(self.host_stt_counts), 'max_mic_seconds': self.pipeline.s.stt_max_seconds}

    def encoded(self, stage):
        with self.lock:
            if self.prefix and self.clip and stage.speaker == 'Ray':
                self.latency['prefix_frames'] += 1
            if self.ray_signal and self.token and self.latency.get('turn_to_program_signal_ms') is None:
                now = time.monotonic()
                self.latency['turn_to_program_signal_ms'] = round(1000*(now-self.turn_started), 2)
                if self.stt_final is not None:
                    self.latency['stt_final_to_program_signal_ms'] = round(1000*(now-self.stt_final), 2)
            self.ray_signal = False
            self.stage = stage
            self.frame += 1
            while self.monitor_history and self.monitor_history[0].end_frame + 4 * FPS <= self.frame:
                self.monitor_history.popleft()

    def close(self):
        self.stopped.set()
        self.handsfree.close()
        self.pipeline.close()
        worker = self.bridge.worker
        if worker:
            worker.join(timeout=self.pipeline.s.turn_timeout + self.pipeline.s.timeout + 1)
        self.bridge.controller.program_pcm = None
        self.stt.close()
