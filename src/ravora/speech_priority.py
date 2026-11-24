"""Scheduled studio arbitration. The encoder remains the single owner of the audio floor."""


class SpeechPriority:
    version = 'short-turn-v1'
    max_seconds = 3.0

    def __init__(self):
        self.waiting_turn = None

    @staticmethod
    def host_speaking(program):
        return program.ingress.locked() or program.handsfree.status()['state'] in ('speaking', 'transcribing')

    @staticmethod
    def prepared_viewer(program):
        pending = program.pipeline.prepared
        return (pending is not None and not pending.discarded and not program._host_interrupt()
                and program.pipeline.inbox.playable(pending.comment['receipt']))

    def host_interrupt(self, program):
        # Let the final encoded packet receive its normal playback acknowledgment.
        if (program.generated and not program.audio and not program.prefix
                and (program.clip is None or program.clip.done)):
            return False
        if program.host_recognition or program.pending_host or program.handsfree.speaking():
            return True
        # The recognizer may still be unwinding the very host turn just admitted.
        return program.pipeline.last_source != 'live-host' and self.host_speaking(program)

    def yield_initiative(self, program, human):
        turn = program.token['turn_id']
        if human:
            self.waiting_turn = turn
        if self.waiting_turn != turn:
            return False
        if program.last_signal is None:
            return True
        # Short streaming-prefix windows are continuations of the same sentence.
        if program.prefix is not None:
            return False
        if program.clip is not None:
            if program.prefetch and program.generated:
                program._prefetch_viewer(sentence_end=True)
            return False
        # A deletion or expiry while the sentence was playing releases the request.
        self.waiting_turn = None
        return (not program._quiet_for_followup(during_playback=True)
                or self.prepared_viewer(program) or program.pipeline.inbox.human_waiting())
