CREATE TABLE twitch_slot_fresh_runs (
    attempt INTEGER PRIMARY KEY,
    scheduled_start INTEGER NOT NULL REFERENCES twitch_schedule_runs(scheduled_start),
    started_at REAL NOT NULL,
    trigger_reason TEXT NOT NULL,
    session TEXT UNIQUE REFERENCES studio_sessions(session),
    finished_at REAL,
    result TEXT NOT NULL CHECK(result IN ('starting','closed','retryable','failed')),
    reason TEXT,
    CHECK((finished_at IS NULL) = (result='starting'))
);
CREATE INDEX twitch_fresh_slot ON twitch_slot_fresh_runs(scheduled_start, attempt);
CREATE UNIQUE INDEX twitch_fresh_open ON twitch_slot_fresh_runs(scheduled_start) WHERE finished_at IS NULL;
CREATE TABLE twitch_fresh_triggers (
    scheduled_start INTEGER PRIMARY KEY REFERENCES twitch_schedule_runs(scheduled_start),
    invocation TEXT NOT NULL,
    reason TEXT NOT NULL,
    observed_at REAL NOT NULL
);
