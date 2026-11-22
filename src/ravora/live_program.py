"""Schedule public host and viewer turns on the headless program timeline."""
from collections import Counter, deque
from dataclasses import asdict, replace
import re
import threading
import time

from ravora.comments import Comment, clean
from ravora.developer_http import RequestError
from ravora.live_media import Clip, FPS, RATE, SILENCE, Stage
from ravora.live_mic import LiveMicrophone
from ravora.live_handsfree import LiveHandsfree
from ravora.live_overlay import ChatLine
from ravora.local_stt import LocalSTT
from ravora.providers import ProviderError
from ravora.ray_character import EXPRESSIONS, MOTIONS
from ravora.stt import AudioPacket


class LiveProgram:
    def __init__(self, pipeline, audio_directory, *, host_language=None, reply_profiles=None,
                 onset_dbfs=None, silence_ms=800):
        self.pipeline = pipeline
        self.bridge = pipeline.bridge
        self.audio_directory = audio_directory
        language = host_language or pipeline.s.stt_language
        self.stt = LocalSTT(pipeline.s)
        self.reply_profiles = reply_profiles or {}
        self.host_reply_language = None
        self.host_stt_counts = Counter()
        self.ingress = threading.Lock()
        self.stopped = threading.Event()
        self.pending_host = None
        self.handsfree = LiveHandsfree(self.submit_audio, pipeline.s.stt_max_seconds,
                                       pipeline.s.handsfree_onset_dbfs if onset_dbfs is None else onset_dbfs,
                                       silence_ms, language=language)
        self.mic = LiveMicrophone(handsfree=self.handsfree)
        self.host_caption = ''
        self.host_caption_until = 0
        self.finished_audio_turn = None
        self.lock = threading.RLock()
        self.stage = Stage()
        self.chat_status = {'status': 'waiting_for_live' if pipeline.s.comment_source == 'youtube' else ('host_only' if pipeline.s.comment_source == 'developer' else 'fixture')}
        self.chat_key = None
        self.chat = ()
        self.chat_ids = ()
        self.selected = self.history_receipt = None
        self.previous = self.last_spoken = ''
        self.history_until = 0
        self.monitor = None
        self.monitor_reset = 0
        self.frame = 0
        self.completed = 0
        self.failed = 0
        self.last_error = None
        self.next_check = 0
        self.token = self.clip = None
        self.audio = deque()
        self.actions = []
        self.cursor = 0
        self.reply_failed = False
        self.generated = False
        self.reaction = 'neutral'
        self.action = ('neutral', 'none')

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
                result = self.stt.transcribe(packet, cancel=cancel or self.stopped, language=language)
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
            comment = Comment('live-host', inbox.session, identifier, 'host', 'Host',
                              clean(result['text'], 1000), time.time())
            clip = None if already_aired else Clip('Host', comment.text, data)
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
                self.host_reply_language = (comment.receipt, self.pipeline.s.language[:2] if language == 'auto' else language)
                if already_aired:
                    self.host_caption = comment.text
                    self.host_caption_until = self.frame + 6 * FPS
                else:
                    self.pending_host = (comment.receipt, clip, time.monotonic() + self.pipeline.s.comment_ttl)
            return {'status': status}
        finally:
            self.ingress.release()

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
        value = self.pipeline.next(allow_host_input=True)
        if not value['available']:
            return
        receipt = value['comment']['receipt']
        language = self.conversation_language
        if self.host_reply_language and self.host_reply_language[0] == receipt:
            _, language = self.host_reply_language
            self.host_reply_language = None
        if self.reply_profiles:
            # next() admits a turn only after the preceding worker and playout finish.
            controller = self.bridge.controller
            controller.s, controller.text, controller.speech = self.reply_profiles[language]
        try:
            self.token = self.pipeline.begin(receipt)
        except ValueError as exc:
            if str(exc) != 'COMMENT_NO_LONGER_AVAILABLE':
                raise
            self.pipeline.inbox.finish(receipt, 'expired')
            return
        self.last_error = None
        self.selected = receipt if value['comment']['source'] in ('youtube', 'youtube-fixture') else None
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

    def _end(self, played=False):
        if not played:
            self.monitor_reset += 1
        self.previous = self.last_spoken if played else ''
        self.history_until = self.frame + 6 * FPS if played else 0
        self.history_receipt = self.selected if played else None
        self.selected = None
        self.last_spoken = ''
        self.finished_audio_turn = self.token['turn_id']
        self.clip = self.token = self.monitor = None
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
            stage, pcm = self._turn_frame()
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
                    self.history_until = self.frame + 6 * FPS
                    self.history_receipt = self.selected
                self.last_spoken = stage.caption
            if self.frame >= self.history_until:
                self.previous = ''
            if self.frame >= self.host_caption_until:
                self.host_caption = ''
            mixed = self.mic.mix(pcm, ray_speaking=stage.speaker == 'Ray', host_playback=stage.speaker == 'Host')
            return replace(stage, chat=self.chat, selected=selected, previous=self.previous,
                           host_caption=self.host_caption, replying=bool(self.last_spoken),
                           thinking=bool(self.token and not stage.speaker and not self.last_spoken)), mixed

    def _turn_frame(self):
        # Only the encoder thread advances this clock. A failed write exits the run
        # before this method can mark the preceding final frame as consumed.
        if self.token is None:
            self._begin()
        if self.token is None:
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
                self.clip = Clip('Ray', text, data, mouth_plan=event.get('mouth_plan',()),
                                 word_starts=event.get('word_starts',()))
                self.monitor = (name, data)
                if index < len(self.actions):
                    self.action = self.actions[index]
        face, motion = self.action
        expression = face if face != 'neutral' else self.reaction
        if self.clip:
            caption, pcm = self.clip.take()
            return Stage(self.clip.speaker, caption, expression, motion,
                         highlight=self.clip.highlight, mouth=self.clip.mouth), pcm
        if self.generated and not self.bridge.worker.is_alive():
            played = self.pipeline.played(self.token)
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
            return {'stage': asdict(self.stage), 'frame': self.frame, 'turns_played': self.completed,
                    'turns_failed': self.failed, 'last_error': self.last_error,
                    'conversation_paused': self.pipeline.paused_reason,
                    'chat': reader.snapshot() if reader else self.chat_status,
                    'reply_mode': self.pipeline.s.mode, 'monitor_reset': self.monitor_reset,
                    'monitor_audio': self.monitor[0] if self.monitor else None,
                    'monitor_offset_seconds': self.clip.position / (2 * RATE) if self.monitor else 0,
                    'handsfree': self.handsfree.status(), 'host_language': self.handsfree.language,
                    'conversation_language': self.conversation_language,
                    'host_stt_counts': dict(self.host_stt_counts), 'max_mic_seconds': self.pipeline.s.stt_max_seconds}

    def encoded(self, stage):
        with self.lock:
            self.stage = stage
            self.frame += 1

    def close(self):
        self.stopped.set()
        self.handsfree.close()
        self.pipeline.close()
        worker = self.bridge.worker
        if worker:
            worker.join(timeout=self.pipeline.s.turn_timeout + self.pipeline.s.timeout + 1)
        self.stt.close()
