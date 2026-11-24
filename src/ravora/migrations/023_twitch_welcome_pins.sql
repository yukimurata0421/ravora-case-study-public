-- One scheduled welcome per slot and per provider-confirmed stream.
CREATE TABLE twitch_welcome_pins (
    scheduled_start INTEGER PRIMARY KEY REFERENCES twitch_schedule_runs(scheduled_start),
    stream_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    message_id TEXT,
    updated REAL NOT NULL
);
