CREATE TABLE twitch_slot_stop_intent (
    scheduled_start INTEGER PRIMARY KEY, observed_at REAL NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('operator','host_shutdown'))
);
CREATE TABLE twitch_boot_attempts (
    attempt INTEGER PRIMARY KEY REFERENCES twitch_slot_fresh_runs(attempt),
    boot_id TEXT NOT NULL, boot_started INTEGER NOT NULL
);
