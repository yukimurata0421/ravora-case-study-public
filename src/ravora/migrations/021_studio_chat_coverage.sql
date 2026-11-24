-- Old sessions have no continuous chat coverage evidence.
ALTER TABLE studio_sessions ADD COLUMN chat_tracked INTEGER NOT NULL DEFAULT 0;
CREATE TABLE studio_chat_intervals (
    session TEXT NOT NULL REFERENCES studio_sessions(session), started REAL NOT NULL,
    ended REAL NOT NULL CHECK(ended >= started), PRIMARY KEY(session, started)
);
CREATE TABLE studio_provider_events (
    session TEXT NOT NULL REFERENCES studio_sessions(session), observed REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('live','ended')), PRIMARY KEY(session, observed)
);
