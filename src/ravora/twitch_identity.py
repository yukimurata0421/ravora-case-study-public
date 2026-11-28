"""Account binding for opt-in scheduled Twitch operations."""
import math
import os
import re
from pathlib import Path

from ravora.settings import read_env


class TwitchError(RuntimeError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.retryable = retryable


def channel_login():
    login = os.environ.get('RAVORA_TWITCH_CHANNEL')
    if login is None:
        login = read_env(Path('.env')).get('RAVORA_TWITCH_CHANNEL', '')
    login = login.strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{1,25}', login):
        raise TwitchError('TWITCH_CHANNEL_LOGIN_REQUIRED')
    return login




def media_generation(transport):
    if not transport or transport.get('state') != 'writing':
        return None
    generation = transport.get('connections_with_media')
    if (type(generation) is int and generation > 0
            and transport.get('last_mux_age_seconds') is not None
            and 0 <= transport['last_mux_age_seconds'] <= 2
            and transport.get('last_tcp_ack_age_seconds') is not None
            and 0 <= transport['last_tcp_ack_age_seconds'] <= 5):
        return generation
    return None


def confirm_rollover(db, session, previous, new, observed, started, before_generation, generation, *, connected_at=None, public_transition=None):
    """Inside the audience writer transaction, after inserting the authoritative Helix sample."""
    from ravora.twitch_schedule import DURATION
    public = public_transition or (None,None,None)
    if public_transition is None:
        if not before_generation or not generation or generation <= before_generation:
            return False
    elif (before_generation is not None or generation != 2 or len(public) != 3
          or not all(isinstance(t,(int,float)) and math.isfinite(t) for t in public)
          or not public[0] < public[1] < public[2] <= observed):
        return False
    if (previous == new
            or not isinstance(started, (int, float)) or not math.isfinite(started) or started > observed):
        return False
    owner = db.execute("""SELECT r.scheduled_start,s.started FROM twitch_scheduled_sessions r
        JOIN studio_sessions s USING(session) WHERE s.session=? AND s.stream_id=? AND s.stopped IS NULL
        AND s.mode='cloud' AND s.platform='twitch' AND s.binding_source='twitch_helix'""",
                       (session, previous)).fetchall()
    if len(owner) != 1:
        return False
    slot_start, studio_start = owner[0]
    if not max(slot_start,studio_start) <= started <= observed < slot_start + DURATION:
        return False
    if db.execute("""SELECT 1 FROM studio_sessions WHERE platform='twitch' AND stream_id=? AND session!=?
        UNION ALL SELECT 1 FROM twitch_stream_rollovers WHERE previous_id=? OR stream_id=? LIMIT 1""",
                  (new, session, new, new)).fetchone():
        return False
    # All live observations since the last accepted transition must describe old -> new
    # in order. Neither stale observations nor a third identity can clear a conflict.
    since, = db.execute('SELECT coalesce(max(first_observed),0) FROM twitch_stream_rollovers WHERE session=?',
                        (session,)).fetchone()
    samples = {row[0]:row[1:] for row in db.execute("""SELECT stream_id,min(observed),max(observed),
        min(stream_started),max(stream_started),count(*)-count(stream_started) FROM studio_audience
        WHERE session=? AND status='live' AND observed>=? GROUP BY stream_id""",(session,since))}
    if set(samples) != {previous,new}:
        return False
    first, latest, low_start, high_start, missing = samples[new]
    if (not samples[previous][1] < started <= first or latest != observed
            or low_start != started or high_start != started or missing):
        return False
    last_old = samples[previous][1]
    if public_transition is None and not (isinstance(connected_at,(int,float)) and last_old <= connected_at <= first):
        return False
    if public_transition and not (studio_start <= public[0] <= started <= public[2]
            and db.execute("""SELECT 1 FROM studio_audience WHERE session=? AND status='offline'
                             AND observed>? AND observed<? LIMIT 1""", (session,samples[previous][1],first)).fetchone()):
        return False
    db.execute('INSERT INTO twitch_stream_rollovers VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (session,previous,new,started,first,observed,before_generation,generation,*public))
    db.execute('UPDATE studio_sessions SET stream_id=?,identity_conflict=0 WHERE session=?',
               (new,session))
    return True


def retired_ids(db, slot_start):
    return {row[0] for row in db.execute("""SELECT previous_id FROM twitch_stream_rollovers
        JOIN twitch_scheduled_sessions USING(session) WHERE scheduled_start=?""",(slot_start,))}
