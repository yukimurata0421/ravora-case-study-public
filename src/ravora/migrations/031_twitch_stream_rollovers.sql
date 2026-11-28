-- One studio encoder may publish several successive provider broadcasts.
CREATE TABLE twitch_stream_rollovers (
    session TEXT NOT NULL REFERENCES studio_sessions(session),
    previous_id TEXT NOT NULL, stream_id TEXT NOT NULL,
    stream_started REAL NOT NULL, first_observed REAL NOT NULL, confirmed_at REAL NOT NULL,
    previous_generation INTEGER, generation INTEGER NOT NULL,
    public_last_old REAL, public_offline REAL, public_first_new REAL,
    PRIMARY KEY(session, stream_id), UNIQUE(session, previous_id),
    CHECK(stream_id != previous_id AND generation >= 2),
    CHECK((previous_generation IS NOT NULL AND previous_generation > 0 AND generation > previous_generation
           AND public_last_old IS NULL AND public_offline IS NULL AND public_first_new IS NULL)
       OR (previous_generation IS NULL AND generation=2 AND public_last_old IS NOT NULL
           AND public_offline IS NOT NULL AND public_first_new IS NOT NULL
           AND public_last_old < public_offline AND public_offline < public_first_new)),
    CHECK(stream_started <= first_observed AND first_observed <= confirmed_at)
);
CREATE VIEW twitch_scheduled_sessions AS
    SELECT scheduled_start,session FROM twitch_schedule_runs WHERE session IS NOT NULL
    UNION ALL SELECT scheduled_start,session FROM twitch_slot_restarts WHERE session IS NOT NULL
    UNION ALL SELECT scheduled_start,session FROM twitch_slot_fresh_runs WHERE session IS NOT NULL;
