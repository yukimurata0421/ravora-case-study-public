"""Local microphone evaluation. No provider calls and no changes to conversation truth."""
from array import array
from dataclasses import asdict, dataclass
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
import wave


class EvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class EvaluationConfig:
    enabled: bool = True
    min_turns: int = 30
    max_turns: int = 50
    max_inputs: int = 100
    max_audio_mib: int = 128
    provenance: str = 'microphone_route'

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise EvaluationError('INVALID_EVALUATION_CONFIG')
        config = cls(**value)
        if (type(config.enabled) is not bool or
                any(type(x) is not int for x in (config.min_turns, config.max_turns, config.max_inputs, config.max_audio_mib)) or
                not 1 <= config.min_turns <= config.max_turns <= config.max_inputs <= 200 or
                not 1 <= config.max_audio_mib <= 256 or config.provenance not in ('microphone_route', 'synthetic_fixture')):
            raise EvaluationError('INVALID_EVALUATION_CONFIG')
        return config


def atomic_json(path, value):
    temporary = path.with_suffix('.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, 'w') as target:
        json.dump(value, target, ensure_ascii=False, allow_nan=False, indent=2)
        target.write('\n')
    temporary.replace(path)


def percentile(values, p):
    values = sorted(x for x in values if x is not None and math.isfinite(x) and x >= 0)
    return round(values[max(0, math.ceil(len(values) * p) - 1)], 3) if values else None


def edit_distance(left, right):
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if not unicodedata.category(c).startswith('P'))


def error_counts(row):
    reference = row.get('reference')
    if reference is None:
        return None
    ref, hyp = normalize(reference), normalize(row.get('transcript') or '')
    chars = ''.join(ref.split()), ''.join(hyp.split())
    words = ref.split(), hyp.split()
    return {'character_edits': edit_distance(*chars), 'reference_characters': len(chars[0]),
            'word_edits': edit_distance(*words), 'reference_words': len(words[0])}


def summarize(rows, config, errors=(), closed=False, limit=None):
    played = [r for r in rows if r['outcome'] == 'played']
    cohorts = {}
    for row in rows:
        group = tuple(row.get(k) for k in ('provenance', 'mode', 'input_source', 'end_mode', 'language', 'stt_model', 'llm_model', 'tts_voice'))
        cohorts.setdefault(group, []).append(row)
    groups = []
    for group, members in cohorts.items():
        complete = [r for r in members if r['outcome'] == 'played']
        timings = {}
        for metric in ('input_end_to_audio_ms', 'stt_final_to_audio_ms', 'last_voice_to_audio_ms'):
            values = [r.get('browser', {}).get(metric) for r in complete]
            timings[metric] = {'n': sum(x is not None for x in values), 'missing': sum(x is None for x in values),
                               'p50': percentile(values, .5), 'p95': percentile(values, .95)}
        reviewed = [r for r in members if r.get('false_endpoint') is not None]
        stt_reviewed = [r for r in members if r.get('stt_error') is not None]
        counts = [v for r in members if (v := error_counts(r)) is not None]
        sums = {k: sum(v[k] for v in counts) for k in ('character_edits', 'reference_characters', 'word_edits', 'reference_words')}
        groups.append({'condition': dict(zip(('provenance', 'mode', 'input_source', 'end_mode', 'language', 'stt_model', 'llm_model', 'tts_voice'), group)),
                       'inputs': len(members), 'played': len(complete), 'latency': timings,
                       'false_endpoint': {'reviewed': len(reviewed), 'unreviewed': len(members) - len(reviewed),
                                          'errors': sum(r['false_endpoint'] for r in reviewed),
                                          'rate': sum(r['false_endpoint'] for r in reviewed)/len(reviewed) if reviewed else None},
                       'stt_error': {'reviewed': len(stt_reviewed), 'unreviewed': len(members) - len(stt_reviewed),
                                     'errors': sum(r['stt_error'] for r in stt_reviewed),
                                     'rate': sum(r['stt_error'] for r in stt_reviewed)/len(stt_reviewed) if stt_reviewed else None},
                       'transcription': {**sums, 'references': len(counts),
                                         'cer': sums['character_edits'] / sums['reference_characters'] if sums['reference_characters'] else None,
                                         'wer': sums['word_edits'] / sums['reference_words'] if sums['reference_words'] else None}})
    return {'schema': 1, 'enabled': True, 'closed': closed, 'config': asdict(config), 'inputs': len(rows),
            'played': len(played), 'target_reached': len(played) >= config.min_turns, 'limit': limit,
            'outcomes': {state: sum(r['outcome'] == state for r in rows) for state in sorted({r['outcome'] for r in rows})},
            'percentile_method': 'nearest_rank; played inputs only; missing values excluded and counted',
            'last_voice_anchor': '20 ms PCM RMS >= -42 dBFS; server receipt minus trailing PCM; approximate, not physical speech end',
            'audio_endpoint': 'first non-silent AudioWorklet notification; not physical speaker output',
            'errors': list(errors), 'cohorts': groups}


def write_report(root, rows, summary):
    atomic_json(root / 'turns.json', rows)
    atomic_json(root / 'summary.json', summary)
    body = ['<!doctype html><html lang="en"><meta charset="utf-8"><title>RAVORA voice evaluation</title>',
            '<h1>RAVORA voice evaluation</h1><p>Private microphone audio and dialogue. Review transcription and endpoint errors manually.</p>',
            '<p>Save a review: ravora-voice-evaluation annotate DIRECTORY --id INPUT_ID --reference CORRECT_TEXT --false-endpoint yes/no</p>',
            '<pre>' + html.escape(json.dumps(summary, ensure_ascii=False, indent=2)) + '</pre>']
    for row in rows:
        body.extend(['<hr><h2>' + html.escape(row['id']) + '</h2>', '<p>' + html.escape(row.get('created_at_jst', '')) + '</p>'])
        if row.get('audio_file'):
            body.append('<audio controls preload="none" src="' + row['audio_file'] + '"></audio>')
        body.append('<pre>' + html.escape(json.dumps(row, ensure_ascii=False, indent=2)) + '</pre>')
    target = root / 'review.new'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, 'w') as stream:
        stream.write('\n'.join(body) + '</html>')
    target.replace(root / 'review.html')


class VoiceEvaluation:
    def __init__(self, root, config, *, mode):
        self.root = Path(root)
        self.config = config
        self.mode = mode
        self.rows = {}
        self.writers = {}
        self.pending = {}
        self.hashes = {}
        self.used_bytes = 0
        self.errors = []
        self.closed = False
        self.limit = None
        source = Path(__file__).resolve().parent
        names = ('voice_evaluation.py', 'fast_voice.py', 'fast_voice_server.py', 'case_study.py',
                 'web/developer/fast-app.js', 'web/developer/evaluation-ui.js')
        self.implementation = {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in names}
        self.root.mkdir(mode=0o2770, parents=True, exist_ok=False)
        # The operator's group must be able to replace reports after DynamicUser exits.
        self.root.chmod(0o2770)
        self.publish()

    def call(self, method, *args, **kwargs):
        """Disk/telemetry failures are visible, but never cancel a conversation."""
        try:
            return getattr(self, method)(*args, **kwargs)
        except (OSError, ValueError, wave.Error) as exc:
            code = type(exc).__name__ + ':' + (str(exc) if isinstance(exc, EvaluationError) else 'EVALUATION_IO_FAILED')
            if code not in self.errors:
                self.errors.append(code)
            return None

    def start(self, key, input_id, session, *, source, end_mode, language, settings):
        if self.closed or self.errors or self.limit:
            return
        if len(self.rows) >= self.config.max_inputs:
            self.limit = 'max_inputs'; self.publish(); return
        if sum(r['outcome'] == 'played' for r in self.rows.values()) >= self.config.max_turns:
            self.limit = 'max_turns'; self.publish(); return
        if not re.fullmatch('[a-f0-9]{64}', key) or key in self.rows:
            raise EvaluationError('INVALID_EVALUATION_INPUT')
        self.rows[key] = {'id': key, 'input_id': input_id, 'session': session, 'mode': self.mode,
                          'provenance': 'mock' if self.mode == 'mock' else self.config.provenance,
                          'created_at_jst': dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(),
                          'input_source': source, 'end_mode': end_mode, 'language': language,
                          'stt_model': settings.voice_stt_model, 'llm_model': settings.gemini_model, 'tts_voice': settings.voice_tts_voice,
                          'input_chunk_ms': settings.voice_input_chunk_ms, 'outcome': 'recording', 'pcm_bytes': 0,
                          'audio_file': None, 'transcript': None, 'reference': None, 'stt_error': None,
                          'false_endpoint': None, 'note': '', 'browser': {}, 'events': []}
        self.publish()

    def pcm(self, key, data):
        row = self.rows.get(key)
        if row is None or row.get('capture_closed') or self.limit == 'max_audio_mib':
            return
        if self.used_bytes + len(data) + 44 > self.config.max_audio_mib * 1024**2:
            self.limit = 'max_audio_mib'
            row['audio_truncated'] = True
            self.end(key, 'evaluation_storage_limit'); return
        if key not in self.writers:
            path = self.root / (key + '.wav')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            handle = os.fdopen(fd, 'wb')
            try:
                writer = wave.open(handle, 'wb'); writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            except BaseException:
                handle.close(); raise
            self.writers[key] = writer, handle
            row['audio_file'] = path.name
            self.used_bytes += 44
        self.writers[key][0].writeframes(data)
        self.writers[key][1].flush()
        self.hashes.setdefault(key,hashlib.sha256()).update(data)
        now = time.monotonic() * 1000
        row['pcm_bytes'] += len(data); self.used_bytes += len(data)
        pending = self.pending.get(key, b'') + data
        while len(pending) >= 640:
            samples = array('h', pending[:640])
            if sys.byteorder != 'little': samples.byteswap()
            pending = pending[640:]
            if sum(x*x for x in samples) >= 320 * 32768**2 * 10**(-42/10):
                row['last_voice_pcm_ms'] = (row['pcm_bytes'] - len(pending)) / 32
                row['last_voice_server_ms'] = now - len(pending) / 32
        self.pending[key] = pending

    def event(self, key, event):
        row = self.rows.get(key)
        if row is not None and len(row['events']) < 100:
            row['events'].append(event)

    def end(self, key, reason):
        row = self.rows.get(key)
        if row is None or row.get('capture_closed'): return
        pair = self.writers.pop(key, None)
        if pair:
            try: pair[0].close()
            finally: pair[1].close()
        self.pending.pop(key, None)
        digest=self.hashes.pop(key,None)
        if digest is not None:row['pcm_sha256']=digest.hexdigest()
        row.update(capture_closed=True, end_reason=reason, input_end_server_ms=time.monotonic()*1000)
        if row['outcome'] == 'recording': row['outcome'] = 'transcribing'
        if row['audio_file']:
            row['audio_sha256'] = hashlib.sha256((self.root / row['audio_file']).read_bytes()).hexdigest()
        self.publish()

    def update(self, key, **fields):
        if key in self.rows:
            if self.rows[key]['outcome']=='played' and fields.get('outcome','played')!='played':
                return  # A late cleanup/cancellation cannot undo an accepted playback ACK.
            self.rows[key].update(fields)
            self.publish()

    def browser(self, key, token, value):
        row = self.rows.get(key)
        if row is None: return
        if row.get('turn_id') != token.get('turn_id'):
            raise EvaluationError('STALE_EVALUATION_TIMING')
        allowed = {'input_end_to_audio_ms', 'stt_final_to_audio_ms', 'signal_server_ms', 'sync_uncertainty_ms', 'underflow_frames'}
        if not isinstance(value, dict) or set(value) != allowed:
            raise EvaluationError('INVALID_EVALUATION_TIMING')
        for name, number in value.items():
            if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1e13):
                raise EvaluationError('INVALID_EVALUATION_TIMING')
        if any(value[name] is not None and value[name]>180000 for name in ('input_end_to_audio_ms','stt_final_to_audio_ms','sync_uncertainty_ms')):
            raise EvaluationError('INVALID_EVALUATION_TIMING')
        if value['underflow_frames'] is not None and type(value['underflow_frames']) is not int:
            raise EvaluationError('INVALID_EVALUATION_TIMING')
        if row['browser']:
            raise EvaluationError('DUPLICATE_EVALUATION_TIMING')
        timing = dict(value)
        timing['last_voice_to_audio_ms'] = None
        if row.get('last_voice_server_ms') is not None and value['signal_server_ms'] is not None and value['sync_uncertainty_ms'] is not None:
            gap = value['signal_server_ms'] - row['last_voice_server_ms']
            if 0 <= gap <= 180000: timing['last_voice_to_audio_ms'] = gap
        row['browser'] = timing
        self.publish()

    def annotate(self, key, fields):
        row = self.rows.get(key)
        if row is None: raise EvaluationError('EVALUATION_INPUT_NOT_FOUND')
        if not isinstance(fields, dict) or set(fields) != {'reference', 'stt_error', 'false_endpoint', 'note'}:
            raise EvaluationError('INVALID_EVALUATION_ANNOTATION')
        if fields['reference'] is not None and (not isinstance(fields['reference'], str) or len(fields['reference']) > 1000):
            raise EvaluationError('INVALID_REFERENCE')
        if any(fields[k] is not None and type(fields[k]) is not bool for k in ('stt_error', 'false_endpoint')):
            raise EvaluationError('INVALID_REVIEW_LABEL')
        if not isinstance(fields['note'], str) or len(fields['note']) > 2000:
            raise EvaluationError('INVALID_REVIEW_NOTE')
        row.update(fields, reviewed_at_jst=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat())
        self.publish()

    def snapshot(self, *, include_rows=False):
        summary = summarize(list(self.rows.values()), self.config, self.errors, self.closed, self.limit)
        summary['implementation_sha256'] = self.implementation
        return {'summary': summary, **({'rows': list(self.rows.values())} if include_rows else {})}

    def publish(self):
        write_report(self.root, list(self.rows.values()), self.snapshot()['summary'])

    def close(self):
        for key in self.rows: self.call('end', key, 'session_closed')
        for row in self.rows.values():
            if row['outcome'] in ('recording', 'transcribing', 'responding', 'generated'):
                row['outcome'] = 'interrupted'
        self.closed = True
        self.call('publish')


def load_evaluation(root):
    root = Path(root).resolve(strict=True)
    previous = json.loads((root / 'summary.json').read_text())
    recorder = VoiceEvaluation.__new__(VoiceEvaluation)
    recorder.root = root
    recorder.config = EvaluationConfig.parse(previous['config'])
    recorder.rows = {r['id']: r for r in json.loads((root / 'turns.json').read_text())}
    recorder.errors = previous['errors']
    recorder.closed = previous['closed']
    recorder.limit = previous['limit']
    recorder.implementation = previous.get('implementation_sha256', {})
    if not recorder.closed:
        raise EvaluationError('Stop the server first. Use the browser review panel while it is running.')
    return recorder


def main():
    """Review saved local evaluations without any network or provider calls."""
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('action', choices=('report', 'annotate'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--id')
    parser.add_argument('--reference', help='Reviewed reference text; an empty string means no speech')
    parser.add_argument('--stt-error', choices=('yes', 'no', 'unknown'))
    parser.add_argument('--false-endpoint', choices=('yes', 'no', 'unknown'))
    parser.add_argument('--note')
    args = parser.parse_args()
    try:
        recorder = load_evaluation(args.directory)
        if args.action == 'annotate':
            if args.id not in recorder.rows:
                parser.error('--id must name an input in turns.json')
            row = recorder.rows[args.id]
            fields = {k: row[k] for k in ('reference', 'stt_error', 'false_endpoint', 'note')}
            if args.reference is not None: fields['reference'] = args.reference
            if args.note is not None: fields['note'] = args.note
            for key in ('stt_error', 'false_endpoint'):
                value = getattr(args, key)
                if value is not None: fields[key] = {'yes': True, 'no': False, 'unknown': None}[value]
            recorder.annotate(args.id, fields)
        else:
            recorder.publish()
        print(json.dumps(recorder.snapshot()['summary'], ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
