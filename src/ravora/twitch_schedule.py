"""Recurring two-hour Ray broadcasts on the JST calendar."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time

from ravora.ray_public_contract import SLOTS
from ravora.youtube_auth import YouTubeError, publisher_lock

JST = timezone(timedelta(hours=9))
DURATION = 2*3600
START_GRACE = 240   # Covers publisher teardown and Twitch disconnect protection.
SAMPLE_PERIOD = 300
DRAIN_SECONDS = 45
# First full 01:00-to-01:00 broadcast day after the rollout.
DAILY_SCHEDULE_START = int(datetime(2026, 11, 25, 9, tzinfo=JST).timestamp())
EXPLORATION_START = int(datetime(2026, 11, 26, 1, tzinfo=JST).timestamp())
EXPLORATION_END = EXPLORATION_START + 14*86400
ARMS = {name: hour for name, hour, _ in SLOTS}

TOPIC_TITLES = (
    ('Add 1 magic key to your keyboard?', 'キーボードに1個だけ謎キーを足すなら？'),
    ('Late snack: sweet or savory?', '夜食は甘い派？しょっぱい派？'),
    ('One real-life Undo button — when do you press it?', '現実世界に「1回だけやり直しボタン」があったら？'),
    ('Upgrade one everyday object?', '日常の道具に1つだけ新機能を足すなら？'),
    ('Invent a comeback rule for losing players', '負けてる側だけ使える必殺ルールを考えよう'),
    ('Best cozy background sound?', 'いちばん落ち着く環境音はどれ？'),
    ('Name a brand-new constellation!', '新しい星座を勝手に名付けるなら？'),
    ('One small thing you tried recently?', '最近ちょっとハマってること・試したこと'),
)


@dataclass(frozen=True)
class Slot:
    start: int
    arm: str

    @property
    def language(self):
        return self.arm[:2]

    @property
    def prompt_title(self):
        start = datetime.fromtimestamp(self.start, JST)
        index = (start.toordinal() * 12 + start.hour // 2) % len(TOPIC_TITLES)
        english, japanese = TOPIC_TITLES[index]
        return japanese if self.language == 'ja' else english

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


def nearby_slots(stamp):
    day = datetime.fromtimestamp(stamp,JST).date()
    return (slots_on(day-timedelta(days=1))[-1], *slots_on(day))


def slot_at(stamp, *, launching=False):
    duration = START_GRACE if launching else DURATION
    return next((s for s in nearby_slots(stamp) if s.start <= stamp < s.start+duration), None)


def sample_slots(stamp):
    return [s for s in nearby_slots(stamp) if s.start <= stamp < s.end+120]


def promotion_time(slot):
    day = datetime.fromtimestamp(slot.start,JST)
    anchor = datetime.fromtimestamp(EXPLORATION_START,JST).date()
    monday = anchor-timedelta(days=anchor.weekday())
    week = (day.date()-monday).days//7
    early = (day.weekday() in (0,2,4,6)) != bool(week%2)
    if day.hour in ((7,21) if early else (11,19)):
        return slot.start+300
    return None


def next_slot(stamp):
    day = datetime.fromtimestamp(stamp,JST).date()
    return next(s for d in (day,day+timedelta(days=1)) for s in slots_on(d) if s.start>stamp)


def predecessor(database, slot):
    with database.connect() as db:
        return db.execute("""SELECT COALESCE((SELECT newer.stream_id
                 FROM twitch_slot_fresh_runs a JOIN studio_sessions newer ON newer.session=a.session
                 WHERE a.scheduled_start=r.scheduled_start AND newer.identity_conflict=0 AND newer.stream_id IS NOT NULL
                 ORDER BY a.attempt DESC LIMIT 1),s.stream_id) FROM twitch_schedule_runs r
             LEFT JOIN twitch_slot_restarts f USING(scheduled_start)
             LEFT JOIN studio_sessions s ON s.session=COALESCE(f.session,r.session)
                                   AND s.platform='twitch' AND s.identity_conflict=0
             WHERE r.scheduled_start=?""",(slot.start-DURATION,)).fetchone()

def slot_held(db, slot):
    return db.execute("SELECT 1 FROM twitch_slot_stop_intent WHERE scheduled_start=? AND kind='operator'",
                      (slot.start,)).fetchone() is not None


def reserve_slot_restart(database, slot, original_session):
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        original = db.execute("""SELECT s.stream_id FROM twitch_schedule_runs r JOIN studio_sessions s USING(session)
            WHERE r.scheduled_start=? AND r.arm=? AND r.session=? AND (r.result='stopped_early' OR (r.result='failed' AND r.reason='PUBLISH_RECOVERY_EXHAUSTED'))
              AND r.finished_at IS NOT NULL AND s.stopped IS NOT NULL AND s.platform='twitch'
              AND s.mode='cloud' AND s.identity_conflict=0 AND s.stream_id IS NOT NULL""",
            (slot.start, slot.arm, original_session)).fetchone()
        if not original or slot_at(time.time()) != slot:
            raise ValueError('TWITCH_SLOT_RESTART_SOURCE_MISMATCH')
        if not db.execute("""INSERT INTO twitch_slot_restarts(scheduled_start,reserved_at)
                VALUES(?,?) ON CONFLICT(scheduled_start) DO NOTHING""",(slot.start,time.time())).rowcount:
            raise ValueError('TWITCH_SLOT_RESTART_ALREADY_RESERVED')
        return original[0]


@contextmanager
def scheduled_lock(directory, slot, previous):
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
