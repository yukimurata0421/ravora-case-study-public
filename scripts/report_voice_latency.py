#!/usr/bin/env python3
"""Summarize acknowledged voice turns without exposing transcripts or identifiers."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ravora.voice_evaluation import percentile


def report(path, limit, mode='cloud'):
    stages = ('first_text_ms', 'public_output_ready_ms', 'first_pcm_ms', 'first_audio_ready_ms')
    result = {}
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        for route, platform in [('studio', 'live-host'), ('developer', 'developer' if mode=='cloud' else 'developer-mock')]:
            # The developer console also accepts typed turns. Include microphone input only.
            rows = db.execute("""SELECT t.metrics,
                    (SELECT i.latency_ms FROM developer_inputs i WHERE i.source_event_id=s.id AND i.kind='audio'
                     AND i.input_state='accepted' ORDER BY i.updated_at DESC LIMIT 1)
                FROM conversation_turns t
                JOIN source_events s ON s.id=t.source_event_id
                WHERE t.played=1 AND t.status='completed' AND s.platform=?
                  AND t.viewer=?||':'||s.viewer_key AND s.is_deleted=0 AND s.raw_purged_at IS NULL
                  AND (s.platform='live-host' OR (json_extract(t.metrics,'$.profile')='fast_streaming' AND EXISTS
                       (SELECT 1 FROM developer_inputs i WHERE i.source_event_id=s.id AND i.kind='audio')))
                ORDER BY t.created_at DESC,t.turn_id DESC LIMIT ?""", (platform,mode,limit)).fetchall()
            turns = [{**json.loads(raw),'recognizer_ms':recognizer} for raw, recognizer in rows]
            shared = ('recognizer_ms',) + stages
            keys = shared + (('stt_ms','turn_to_program_signal_ms','stt_final_to_program_signal_ms') if route=='studio' else ())
            timing = {}
            for key in keys:
                values = [t.get(key) if key in shared else t.get('live_program',{}).get(key) for t in turns]
                timing[key] = {'n':sum(v is not None for v in values), 'missing':values.count(None),
                               'p50':percentile(values,.5), 'p95':percentile(values,.95)}
            result[route] = {'played_audio_turns':len(turns), 'milliseconds':timing}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--mode',choices=('cloud','offline'),default='cloud')
    parser.add_argument('--limit',type=int,default=60,help='Recent turns per path')
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000:
        parser.error('--limit must be between 1 and 10000')
    print(json.dumps(report(args.database,args.limit,args.mode),indent=2))
