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
from ravora.live_media import Clip, FPS, RATE, SAMPLES, SILENCE, SpeechPrefix, Stage
from ravora.live_mic import LiveMicrophone
from ravora.live_reverb import DEFAULT_RAY_REVERB_MIX, RayReverb
from ravora.live_stt import CloudHostSTT
from ravora.live_handsfree import LiveHandsfree
from ravora.live_overlay import ChatLine, caption_layout
from ravora.local_stt import LocalSTT
from ravora.providers import ProviderError
from ravora.ray_character import EXPRESSIONS, MOTIONS
from ravora.stt import AudioPacket
from ravora.twitch_voice import profile_live_turn, refresh_english_voice


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
                 ray_reverb_mix=DEFAULT_RAY_REVERB_MIX, bgm=None, prefetch=False, initiative_language=None,
                 interrupt_initiatives=False):
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
        self.initiative_language = initiative_language or 'en'
        self.slot_draining = False
        self.drain_host = False
        self.interrupt_initiatives = interrupt_initiatives
        self.speech_policy = None
        self.policy_generation = 0
        if interrupt_initiatives:
            from ravora.speech_priority import SpeechPriority
            self.speech_policy = SpeechPriority()
            tail = self.reverb.tail_samples / RATE if self.reverb.mix else 0
            self.bridge.controller.initiative_audio_limit = self.speech_policy.max_seconds - tail
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
        self.observations = None
        self.prefetch = prefetch
        self.prefetch_revision = None
        self.prefetched_comment = None
        self.viewer_receipt = None
        self.frame_signal = self.last_signal = self.last_audible_turn = None
        self.stage = Stage()
        self.chat_status = {'status': {'youtube': 'waiting_for_live', 'twitch': 'connecting',
                                       'developer': 'host_only'}.get(pipeline.s.comment_source, 'fixture')}
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
        self.twitch_intake = None
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

    def observe(self, platform, stream_id=None, binding_source=None):
        from ravora.studio_observations import StudioObservations
        self.observations = StudioObservations(self.pipeline.inbox, platform, self.prefetch,
                                               self.pipeline.s.comment_source, stream_id, binding_source,
                                               sample_rate=RATE, fps=FPS)
        if self.speech_policy:
            self.observations.speech_policy(self.frame,self.speech_policy.version)
        return self.observations

    _profile = profile_live_turn

    def _prefetch_viewer(self, *, sentence_end=False):
        if self.slot_draining:
            return
        pipeline = self.pipeline
        pending = pipeline.prepared
        if pending:
            if not pending.discarded:
                if pipeline.inbox.clock() >= pending.comment['expires']:
                    pipeline.discard_prepared('queue_expired')
                elif self._host_interrupt():
                    pipeline.discard_prepared('host_interrupt')
                elif not pending.bridge.worker.is_alive() and not any(e['kind']=='complete' for e in pending.bridge.poll(0)['events']):
                    pipeline.discard_prepared('preparation_failed')
            pipeline.reap_prepared()
        elif (self.token and self.generated and self.last_signal and not self.stopped.is_set()
              and not self.bridge.worker.is_alive() and not self._host_interrupt()):
            # No SQLite polling at the encoder cadence. Recheck when the viewer queue changes.
            revision = pipeline.inbox.display_revision
            if self.prefetch_revision != revision:
                self.prefetch_revision = revision
                remaining = sum(event['duration'] for event in self.audio) if not sentence_end else 0
                if self.prefix and self.audio and self.audio[0]['index']==0:
                    remaining -= self.prefix.consumed / (2*RATE)
                if self.clip:
                    remaining += (len(self.clip.playout_pcm)-self.clip.position) / (2*RATE)
                pipeline.prepare_viewer(lambda bridge, comment: self._profile(bridge, comment, self.conversation_language),
                                        max(0,remaining))

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

    def finish_slot_turn(self):
        """At the slot boundary, finish the current human turn and a host input already underway."""
        with self.lock:
            if not self.slot_draining:
                self.slot_draining = True
                self.drain_host = bool(self.host_recognition or self.pending_host or self.speech_policy.host_speaking(self))
                self.pipeline.discard_prepared('slot_ended')
            host_in_progress = self.drain_host and (self.host_recognition or self.pending_host or self.speech_policy.host_speaking(self))
            return bool((self.token and not self._interruptible_initiative()) or host_in_progress)

    def _begin(self):
        if self.twitch_intake:
            self.twitch_intake.wake_idle(self)
        if self.slot_draining:
            self.pipeline.reap_prepared()
            if not self.drain_host or not self.host_recognition:
                return
        if self.pending_host and time.monotonic() >= self.pending_host[2]:
            self.pending_host = None
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + .12
        if self.bridge is not self.pipeline.bridge:
            # A cancelled opening may still be inside an already charged provider call.
            self.bridge = self.pipeline.bridge
            self.finished_audio_turn = None
        self.pipeline.reap_initiative()
        if self.finished_audio_turn and self.bridge.worker and not self.bridge.worker.is_alive():
            for path in self.audio_directory.glob(self.finished_audio_turn + '-*'):
                path.unlink(missing_ok=True)
            self.finished_audio_turn = None
        self.initiative_revision = (self.pipeline.inbox.display_revision,self.host_recognition)
        if self.speech_policy:
            if self.host_recognition and time.monotonic() - self.host_recognition[2] >= self.pipeline.s.comment_ttl:
                self.host_recognition = None
            if not self.host_recognition and self.speech_policy.host_speaking(self):
                if self.bridge.controller.character:
                    self.bridge.controller.character.note_mic_activity(speaking=True)
                return
        quiet = self._quiet_for_followup()
        if not quiet and self.bridge.controller.character:
            self.bridge.controller.character.note_mic_activity()
        pending = self.pipeline.promote() if self.pipeline.prepared else None
        if pending:
            self.bridge = pending.bridge
            comment = pending.comment
        else:
            if self.pipeline.prepared:
                return
            drain_args = {'only_receipt':self.host_recognition[0]} if self.slot_draining else {}
            value = self.pipeline.next(allow_host_input=True, allow_initiative=quiet and not self.slot_draining,
                                       prefer_host=self.interrupt_initiatives, **drain_args)
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
            self.drain_host = False
        self.prefetched_comment = comment if pending else None
        self.viewer_receipt = receipt if viewer_comment else None
        self.prefetch_revision = None
        if pending:
            self.turn_started = pending.started
            self.token = pending.token
            self.prefix = pending.prefix
        else:
            self._profile(self.bridge, comment, language)
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
        self.selected = receipt if viewer_comment and comment.get('reply_kind')!='group' else None
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

    def _host_interrupt(self):
        return (self.ingress.locked() or self.pending_host or self.host_recognition
                or not self.mic.allows_initiative(during_playback=True))

    def _quiet_for_followup(self, *, during_playback=False):
        return not (self.ingress.locked() or self.pending_host) and self.mic.allows_initiative(
            during_playback=during_playback, require_echo_cancellation=self.interrupt_initiatives)

    def restart_speech_policy(self):
        """Swap the studio's turn arbiter between frames, retaining the live writer and its active turn."""
        import importlib
        from ravora import speech_priority
        with self.lock:
            replacement = importlib.reload(speech_priority).SpeechPriority()
            refresh_english_voice(self)
            if self.observations and replacement.version != self.speech_policy.version:
                self.observations.speech_policy(self.frame,replacement.version)
            self.speech_policy = replacement
            self.policy_generation += 1

    def _interruptible_initiative(self):
        return self.interrupt_initiatives and self.pipeline.active_followup is not None

    def _receive_pcm(self, turn_id, text, packet):
        with self.lock:
            if self.stopped.is_set():
                return
            pending = self.pipeline.prepared
            if pending and pending.token['turn_id'] == turn_id:
                if pending.discarded or not pending.bridge.fence.current(pending.token):
                    return
                if pending.prefix is None:
                    pending.prefix = SpeechPrefix(text)
                prefix = pending.prefix
            elif self.token and self.token['turn_id'] == turn_id and self.bridge.fence.current(self.token):
                if self.prefix is None:
                    self.prefix = SpeechPrefix(text)
                prefix = self.prefix
            else:
                return
            was_ready = prefix.consumed or len(prefix.pending) >= prefix.window
            prefix.feed(packet)
            if self.observations and not was_ready and len(prefix.pending) >= prefix.window:
                self.observations.ready(turn_id, time.time())

    def _flush_signal(self):
        if self.observations and self.last_signal:
            self.observations.end_signal(self.token['turn_id'], *self.last_signal)
        self.last_signal = None

    def _end(self, played=False, *, keep_prepared=False):
        self._flush_signal()
        self.prefetched_comment = None
        if self.pipeline.prepared:
            if not played and not keep_prepared:
                self.pipeline.discard_prepared('preceding_turn_interrupted')
            self.next_check = 0
        if not played:
            self.monitor_reset += 1
            self.monitor_history.clear()
        self.previous = self.last_spoken if played else ''
        self.previous_font = self.last_spoken_font
        self.history_until = self.frame + 6 * FPS if played else 0
        self.history_receipt = self.viewer_receipt if played else None
        self.viewer_receipt = None
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
                if self.viewer_receipt and event['index']==0 and 'elapsed_ms' in event:
                    self.pipeline.preparation_seconds.append((time.monotonic(),event['elapsed_ms']/1000))
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
            # Keep a new human receipt queued until the unsolicited turn yields.
            # Cancelling an active turn also discards any reply already prefetched behind it.
            if self.speech_policy and self.token and (not self.slot_draining or self.drain_host) and self.speech_policy.host_interrupt(self):
                self.pipeline.cancel('host_interrupt')
                self._end()
                self.next_check = 0
            if self.prefetch and not self._interruptible_initiative():
                self._prefetch_viewer()
            stage, pcm = self._turn_frame()
            # Measure Ray before mixing the host. Count the frame only after writer.write succeeds.
            self.frame_signal = None
            if stage.speaker == 'Ray':
                indices = np.flatnonzero(np.abs(np.frombuffer(pcm,'<i2').astype(np.int32)) >= 33)
                if indices.size:
                    self.frame_signal = (self.frame*SAMPLES + int(indices[0]), self.frame*SAMPLES + int(indices[-1]) + 1)
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
                    self.history_receipt = self.viewer_receipt
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
                                                self.token['turn_id'] if self.viewer_receipt or self.selected else None, self.monitor[1]))

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
        if self.prefetched_comment and self.last_signal is None:
            comment = self.prefetched_comment
            reason = ('queue_expired' if self.pipeline.inbox.clock() >= comment['expires'] else
                      'host_interrupt' if self._host_interrupt() else None)
            if reason:
                self.pipeline.cancel(reason)
                self._end()
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
        waiting_for_ack = self.generated and not (self.clip or self.audio or self.prefix)
        if self.initiative_waiting or (self._interruptible_initiative() and not waiting_for_ack):
            inbox = self.pipeline.inbox
            revision = (inbox.display_revision,self.host_recognition)
            changed = self.initiative_revision != revision
            self.initiative_revision = revision
            human = (not self._quiet_for_followup(during_playback=not self.initiative_waiting)
                     or (changed and inbox.human_waiting()))
            if self.speech_policy:
                human = self.speech_policy.yield_initiative(self, human)
            if human or (self.last_signal is None and not self.pipeline.audience_allows_initiative()):
                keep = bool(human and self.speech_policy and self.speech_policy.prepared_viewer(self))
                self.pipeline.cancel('human_input_priority' if human else 'audience_unavailable', keep_prepared=keep)
                self._end(keep_prepared=keep)
                self.next_check = 0
                return Stage(), SILENCE
        if (self.initiative_waiting and self.speech_policy and self.audio
                and (not self.generated or self.bridge.worker.is_alive())):
            # A short opening becomes audible only when its single WAV is accepted
            # and the speech worker is free to start a viewer reply on the next frame.
            return Stage(), SILENCE
        # Initial playout checks playable() below. Recheck deleted group/individual
        # sources before claiming each subsequent sentence that already has a WAV.
        if (self.viewer_receipt and self.last_signal is not None and self.clip is None and self.audio
                and self.pipeline.inbox.removed_turn(self.token['turn_id'])):
            self.pipeline.cancel('unavailable_before_playout')
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
            if self.viewer_receipt and self.last_signal is None and not self.pipeline.inbox.playable(self.viewer_receipt):
                self.pipeline.cancel('unavailable_before_playout')
                self._end()
                return Stage(), SILENCE
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
                    'reply_mode': self.pipeline.s.mode, 'interrupt_initiatives': self.interrupt_initiatives,
                    'speech_policy_generation': self.policy_generation,
                    'monitor_reset': self.monitor_reset,
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
            if self.frame_signal and self.token:
                first, end = self.frame_signal
                stamp = time.time()
                if self.last_signal is None:
                    now = time.monotonic()
                    self.latency['turn_to_program_signal_ms'] = round(1000*(now-self.turn_started), 2)
                    if self.stt_final is not None:
                        self.latency['stt_final_to_program_signal_ms'] = round(1000*(now-self.stt_final), 2)
                    if self.observations:
                        self.observations.first_signal(self.token['turn_id'], self.last_audible_turn, first, stamp)
                    self.last_audible_turn = self.token['turn_id']
                self.last_signal = end, stamp
            self.frame_signal = None
            self.stage = stage
            self.frame += 1
            if self.observations:
                reader = self.pipeline.reader
                self.observations.chat_sample(reader is not None and reader.status == 'reading', time.time())
            while self.monitor_history and self.monitor_history[0].end_frame + 4 * FPS <= self.frame:
                self.monitor_history.popleft()

    def close(self):
        self.stopped.set()
        self.handsfree.close()
        with self.lock:
            self._flush_signal()
        self.pipeline.close()
        worker = self.bridge.worker
        if worker:
            worker.join(timeout=self.pipeline.s.turn_timeout + self.pipeline.s.timeout + 1)
        self.bridge.controller.program_pcm = None
        self.stt.close()
        if self.observations:
            reader = self.pipeline.reader
            self.observations.close(self.frame, reader.status if reader else self.chat_status['status'])
