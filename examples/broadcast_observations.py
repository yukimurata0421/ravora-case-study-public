"""Write a fresh public export of a fictional, completed 17:00 JST broadcast."""
from datetime import datetime
from pathlib import Path
import tempfile
import time

from ravora.database import Database
from ravora.ray_public import dumps, project
from ravora.twitch_schedule import JST


def example_export():
    start = datetime(2026, 1, 6, 17, tzinfo=JST).timestamp()
    end = start + 7200
    with tempfile.TemporaryDirectory() as folder:
        store = Database(Path(folder) / 'fictional.sqlite3')
        with store.connect() as db:
            db.execute("""INSERT INTO studio_sessions
                (session, platform, mode, sample_rate, fps, prefetch, chat_source,
                 chat_status_at_stop, started, stopped, frames, stream_id, binding_source, chat_tracked)
                VALUES ('demo','twitch','offline',48000,30,1,'twitch','stopped',?,?,216000,'show','twitch_helix',1)""",
                       (start, end))
            db.execute("INSERT INTO studio_chat_intervals VALUES ('demo',?,?)", (start, end))
            db.execute("INSERT INTO studio_stream_summaries VALUES ('twitch','show',?,?,5,2,3)", (start, end))
            db.execute("INSERT INTO twitch_schedule_runs VALUES (?,'ja-17',?,?,'finished',NULL,'demo')",
                       (start, start, end))
            # The second comment is waiting at the previous audio end. The third arrives later.
            times = [(0.5, 1.0, 2.0), (1.7, 2.25, 3.0), (4.0, 5.0, 6.0)]
            prior = None
            for index, (arrival, first, last) in enumerate(times):
                turn = f'reply-{index}'
                db.execute("""INSERT INTO studio_inputs
                    (receipt,session,source,viewer,published,received,status,selected,turn_id,played_at,
                     ready_at,previous_turn,first_sample,end_sample,first_written,last_written)
                    VALUES (?,'demo','twitch','fictional-viewer',?,?,'played',1,?,?,?,?,?,?,?,?)""",
                           (f'message-{index}', start + arrival, start + arrival, turn, start + last,
                            start + first - .1, prior, int(first*48000), int(last*48000), start + first, start + last))
                prior = turn
        with Database.existing(store.path, readonly=True).connect() as db:
            db.execute('BEGIN')
            return project(db, time.time())


if __name__ == '__main__':
    print(dumps(example_export()))
