-- Claim a slot durably before any channel update or RTMPS publisher is opened.
CREATE TABLE twitch_schedule_runs (
    scheduled_start INTEGER PRIMARY KEY,
    arm TEXT NOT NULL,
    claimed_at REAL NOT NULL, finished_at REAL, result TEXT NOT NULL, reason TEXT,
    session TEXT UNIQUE REFERENCES studio_sessions(session)
);
-- One attempted sample per planned five-minute tick, independent of studio/chat availability.
CREATE TABLE twitch_schedule_market (
    scheduled_start INTEGER NOT NULL, tick INTEGER NOT NULL, sampled REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('complete','partial','error')),
    pages INTEGER NOT NULL, channels INTEGER, viewers INTEGER, top5_viewers INTEGER,
    peers_at REAL, ray_stream TEXT, rival_stream TEXT,
    PRIMARY KEY(scheduled_start,tick)
);
