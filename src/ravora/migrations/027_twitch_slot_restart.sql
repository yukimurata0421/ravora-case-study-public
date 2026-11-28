-- An operator restart keeps the original scheduled run and its observations intact.
CREATE TABLE twitch_slot_restarts (
    scheduled_start INTEGER PRIMARY KEY REFERENCES twitch_schedule_runs(scheduled_start),
    reserved_at REAL NOT NULL,
    session TEXT UNIQUE REFERENCES studio_sessions(session)
);
