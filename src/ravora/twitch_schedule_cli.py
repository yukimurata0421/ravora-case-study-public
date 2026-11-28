#!/usr/bin/env python3
"""Plan or run the current Twitch slot and its independent market observer."""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time


from ravora.database import Database
from ravora.twitch_schedule import JST, SAMPLE_PERIOD, ARMS, next_slot, sample_slots, slot_at, slots_on
from ravora.twitch_identity import TwitchError, channel_login
from ravora.youtube_auth import publisher_lock

ROOT=Path.cwd()
DB=ROOT/'state/conversation.sqlite3'
DIRECTORY=ROOT/'state/twitch'


def launch(slot, database, studio, *, clock=time.time):
    with database.connect() as db:
        claimed=db.execute("""INSERT INTO twitch_schedule_runs(scheduled_start,arm,claimed_at,result)
            VALUES(?,?,?,'claimed') ON CONFLICT(scheduled_start) DO NOTHING""",
            (slot.start,slot.arm,clock())).rowcount
    if not claimed:
        return 'already_claimed'
    result, reason='interrupted', None
    try:
        config = ['--config', 'studio.toml'] if Path('studio.toml').is_file() else []
        studio([*config,'--mode','cloud','--publish-twitch','--host-language',slot.language,'--prefetch',
                '--seconds',str(max(1,slot.end-int(clock()))),'--port','0'], scheduled=slot)
        if clock() < slot.end-2:
            result = 'stopped_early'
        else:
            with database.connect() as db:
                confirmed = db.execute('''SELECT 1 FROM twitch_schedule_runs r JOIN studio_sessions s USING(session)
                    JOIN studio_audience a ON a.session=s.session AND a.stream_id=s.stream_id AND a.status='live'
                    WHERE r.scheduled_start=? AND s.identity_conflict=0 LIMIT 1''', (slot.start,)).fetchone()
            result = 'finished' if confirmed else 'unconfirmed'
    except (SystemExit, Exception) as exc:
        code=str(exc)
        reason=code if code.isascii() and code.replace('_','').isalnum() and code.isupper() and len(code)<80 else 'STUDIO_FAILED'
        result='blocked' if reason in ('RAY_PUBLISHER_ALREADY_RUNNING','TWITCH_CHANNEL_ALREADY_LIVE',
                                       'TWITCH_CHANNEL_AUTHORIZATION_REQUIRED','TWITCH_SLOT_EXPIRED') else 'failed'
        raise
    finally:
        with database.connect() as db:
            db.execute('UPDATE twitch_schedule_runs SET result=?,reason=?,finished_at=? WHERE scheduled_start=?',
                       (result,reason,clock(),slot.start))
    return result


def collect(slots, dbfile, stamp):
    from ravora.twitch_market import sample

    if not slots:
        return 'outside_schedule'
    database = Database(dbfile)
    pending = []
    with database.connect() as db:
        for slot in slots:
            tick = int(stamp-slot.start) // SAMPLE_PERIOD
            recorded = db.execute('SELECT 1 FROM twitch_schedule_market WHERE scheduled_start=? AND tick=?',
                                  (slot.start, tick)).fetchone()
            if not recorded:
                pending.append((slot, tick))
    if not pending:
        return 'already_sampled'
    # Two adjacent slots can use different Twitch category languages.
    observations = {}
    for slot, tick in pending:
        if slot.language not in observations:
            observations[slot.language] = sample(DIRECTORY, slot)
        market, peers = observations[slot.language]
        with database.connect() as db:
            db.execute('INSERT INTO twitch_schedule_market VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                       (slot.start, tick, stamp, *market, *peers))
    if any(market[0]=='error' or peers[0] is None for market,peers in observations.values()):
        raise TwitchError('TWITCH_MARKET_SAMPLE_INCOMPLETE')
    return 'partial' if any(market[0]=='partial' for market,_ in observations.values()) else 'complete'


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('plan','check','authorize','authorize-pin','authorize-irc','run','sample'))
    parser.add_argument('--hour',type=int,choices=tuple(ARMS.values()))
    args=parser.parse_args(argv)
    previous_mask = os.umask(0o077)
    try:
        return run_command(args)
    finally:
        os.umask(previous_mask)


def run_command(args):
    now=time.time()
    if args.command=='plan':
        day=datetime.fromtimestamp(now,JST).date()
        for delta in range(7):
            for slot in slots_on(day+timedelta(days=delta)):
                print(slot.start_jst,slot.arm)
        slot=next_slot(now)
        print('Next:',slot.start_jst,slot.arm)
        return
    channel_login()
    if args.command=='authorize-irc':
        from ravora.twitch_schedule_api import authorize_channel
        authorize_channel(DIRECTORY/'irc-oauth.json', ('chat:read',))
        print('TWITCH_IRC_READ_AUTHORIZED')
        return
    if args.command=='authorize-pin':
        from ravora.twitch_schedule_api import authorize_pin
        authorize_pin(DIRECTORY)
        print('TWITCH_WELCOME_PIN_AUTHORIZED')
        return
    if args.command=='authorize':
        from ravora.twitch_schedule_api import authorize_metadata
        authorize_metadata(DIRECTORY)
        print('TWITCH_SCHEDULE_AUTHORIZED')
        return
    if args.command=='check':
        from ravora.twitch_schedule_api import prepare_channel
        with publisher_lock(ROOT/'state/youtube',busy_code='RAY_PUBLISHER_ALREADY_RUNNING'):
            print(json.dumps(prepare_channel(DIRECTORY,next_slot(now).language,update=False)))
        return
    if args.command=='sample':
        print(collect(sample_slots(now), DB, now))
        return
    slot=slot_at(now,launching=True)
    if slot and (args.hour is None or ARMS[slot.arm]==args.hour):
        from ravora import youtube_live
        print(launch(slot,Database(DB),youtube_live.main))



def entrypoint():
    try:
        main()
    except Exception as exc:
        code=str(exc)
        sys.exit(code if isinstance(exc,TwitchError) or (code.isupper() and code.replace('_','').isalnum()) else 'TWITCH_SCHEDULE_FAILED')


if __name__ == '__main__':
    entrypoint()
