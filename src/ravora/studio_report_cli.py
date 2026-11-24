#!/usr/bin/env python3
"""Report broadcast observations, trace program clocks and import measurements."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys


from ravora.database import Database
from ravora.studio_observations import import_observation
from ravora.studio_report import first_ready, jst, report, timeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('state/conversation.sqlite3'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('report', help='Print the private per-broadcast report as JSON')
    trace = sub.add_parser('timeline', help='Show receipt, preparation and program times for one local session')
    trace.add_argument('session')
    match = sub.add_parser('match-player', help='Align independent player audio with a reply')
    match.add_argument('session')
    match.add_argument('turn_id')
    match.add_argument('capture', type=Path)
    match.add_argument('--capture-start', type=float, required=True, help='Unix time of the first recorded sample')
    match.add_argument('--reference', type=Path, help='Saved first WAV of this turn')
    ingest = sub.add_parser('import', help='Import a platform summary, player match or test label')
    ingest.add_argument('file', type=Path)
    args = parser.parse_args()
    try:
        store = Database.existing(args.db, readonly=args.command!='import')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE' if args.command=='import' else 'BEGIN')
            if args.command=='report':
                result = report(db)
            elif args.command=='match-player':
                from ravora.player_match import player_observation
                result = player_observation(db,args.session,args.turn_id,args.capture,args.capture_start,args.reference)
            elif args.command=='timeline':
                db.row_factory = sqlite3.Row
                if not db.execute('SELECT 1 FROM studio_sessions WHERE session=?',(args.session,)).fetchone():
                    raise ValueError('UNKNOWN_STUDIO_SESSION')
                rows = timeline(db,json.dumps([args.session]))
                result = {'inputs':[{'receipt':r['receipt'],'turn_id':r['turn_id'],'source':r['source'],'status':r['status'],
                                     'received_jst':jst(r['received']), 'audio_ready_jst':jst(first_ready(r)),
                                     'first_sample':r['first_sample'], 'end_sample_exclusive':r['end_sample'],
                                     'first_write_jst':jst(r['first_written']), 'last_write_jst':jst(r['last_written'])}
                                    for r in rows],
                          'audience':[{'observed_jst':jst(t),'status':status,'stream_id':stream,'viewers':v} for t,status,stream,v in db.execute(
                              'SELECT observed,status,stream_id,viewers FROM studio_audience WHERE session=? ORDER BY observed',
                              (args.session,))]}
            else:
                records = json.loads(args.file.read_text())
                if not isinstance(records,list):
                    records = [records]
                for r in records:
                    if not isinstance(r,dict):
                        raise ValueError('INVALID_OBSERVATION')
                    import_observation(db,r)
                result = {'accepted_records':len(records)}
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        parser.exit(1, f'{exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__=='__main__':
    main()
