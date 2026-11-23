"""Seed a source-bound conversation turn using columns present since migration 001."""
from datetime import datetime, timezone


def insert_legacy_turn(store, comment, turn_id, reply, *, played):
    now = datetime.now(timezone.utc)
    state = 'COMPLETED' if played else 'SELECTING' if reply is None else 'GENERATED'
    with store.connect() as db:
        db.execute("""INSERT INTO conversation_turns
            (id,turn_id,source_event_id,viewer,comment,reply,status,played,started,state,created_at,closed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (turn_id, turn_id, comment.receipt, 'offline:' + comment.viewer, comment.text, reply or '',
             'running' if reply is None else 'completed', int(played), now.isoformat(), state,
             now.timestamp(), now.timestamp() if played else None))
