"""Recurring two-hour Ray broadcasts on the JST calendar."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time

from ravora.ray_public_contract import SLOTS

JST = timezone(timedelta(hours=9))
DURATION = 2*3600
START_GRACE = 240   # Covers publisher teardown and Twitch disconnect protection.
SAMPLE_PERIOD = 300
ARMS = {name: hour for name, hour, _ in SLOTS}


@dataclass(frozen=True)
class Slot:
    start: int
    arm: str

    @property
    def language(self):
        return self.arm[:2]

    @property
    def end(self):
        return self.start + DURATION

    @property
    def start_jst(self):
        return datetime.fromtimestamp(self.start, JST).isoformat(timespec='minutes')


def slots_on(day):
    midnight=datetime.combine(day,datetime.min.time(),tzinfo=JST)
    return tuple(Slot(int((midnight+timedelta(hours=hour)).timestamp()),arm)
                 for arm,hour,days in SLOTS if day.weekday() in days)


def slot_at(stamp, *, launching=False):
    for slot in slots_on(datetime.fromtimestamp(stamp,JST).date()):
        if slot.start <= stamp < slot.start + (START_GRACE if launching else DURATION):
            return slot
    return None


def sample_slots(stamp):
    return [s for s in slots_on(datetime.fromtimestamp(stamp,JST).date()) if s.start<=stamp<s.end+120]


def next_slot(stamp):
    day = datetime.fromtimestamp(stamp,JST).date()
    return next(s for d in (day,day+timedelta(days=1)) for s in slots_on(d) if s.start>stamp)


def predecessor(database, slot):
    with database.connect() as db:
        return db.execute("""SELECT s.stream_id FROM twitch_schedule_runs r
             JOIN studio_sessions s ON s.session=r.session AND s.platform='twitch' AND s.identity_conflict=0
             WHERE r.scheduled_start=?""",(slot.start-DURATION,)).fetchone()


@contextmanager
def scheduled_lock(directory, slot, previous):
    from ravora.youtube_auth import YouTubeError, publisher_lock

    while True:
        lock=publisher_lock(directory,busy_code='RAY_PUBLISHER_ALREADY_RUNNING')
        try:
            lock.__enter__()
            break
        except YouTubeError:
            if not previous or time.time()+2>=slot.start+START_GRACE:
                raise
            time.sleep(2)
    try:
        yield
    finally:
        lock.__exit__(None,None,None)
