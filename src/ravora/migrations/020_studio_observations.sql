-- A program clock starts afresh on every launch, including reconnections to the same broadcast.
CREATE TABLE studio_sessions (
    session TEXT PRIMARY KEY, platform TEXT NOT NULL, mode TEXT NOT NULL,
    sample_rate INTEGER NOT NULL CHECK(sample_rate>0), fps INTEGER NOT NULL CHECK(fps>0),
    prefetch INTEGER NOT NULL, chat_source TEXT NOT NULL, chat_status_at_stop TEXT, timing_write_losses INTEGER, started REAL NOT NULL, stopped REAL, frames INTEGER,
    stream_id TEXT, binding_source TEXT, identity_conflict INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX studio_usage_session ON conversation_turns(usage_session);
CREATE INDEX studio_usage_turn ON api_usage(turn_id);
CREATE INDEX studio_broadcast ON studio_sessions(platform,stream_id);

-- Numeric observations outlive the one-day inbox. No message, name, audio or credentials.
CREATE TABLE studio_inputs (
    receipt TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES studio_sessions(session),
    source TEXT NOT NULL, viewer TEXT NOT NULL, published REAL NOT NULL, received REAL NOT NULL,
    status TEXT NOT NULL, reason TEXT, intake_reason TEXT, selected INTEGER NOT NULL DEFAULT 0, turn_id TEXT UNIQUE,
    prefetched INTEGER NOT NULL DEFAULT 0, test INTEGER NOT NULL DEFAULT 0, played_at REAL,
    ready_at REAL, previous_turn TEXT, first_sample INTEGER, end_sample INTEGER, first_written REAL, last_written REAL,
    player_at REAL, player_correlation REAL, player_capture_sha256 TEXT
);
CREATE INDEX studio_inputs_session ON studio_inputs(session,received);
CREATE TRIGGER studio_input_received AFTER INSERT ON comment_inbox
WHEN EXISTS (SELECT 1 FROM studio_sessions WHERE session=NEW.session)
BEGIN
    INSERT INTO studio_inputs(receipt,session,source,viewer,published,received,status,reason,intake_reason,test)
    VALUES(NEW.receipt,NEW.session,NEW.source,NEW.viewer,NEW.published,NEW.received,NEW.status,NEW.reason,NEW.reason,
           NEW.source IN ('fixture','youtube-fixture','memory-fixture'));
END;
CREATE TRIGGER studio_input_changed AFTER UPDATE OF status,turn_id ON comment_inbox
BEGIN
    UPDATE studio_inputs SET status=NEW.status,reason=NEW.reason,turn_id=NEW.turn_id,
        selected=MAX(selected,NEW.status='claimed')
    WHERE receipt=NEW.receipt;
END;
CREATE TRIGGER studio_playout_ack AFTER UPDATE OF played ON conversation_turns
WHEN OLD.played=0 AND NEW.played=1
BEGIN
    UPDATE studio_inputs SET played_at=COALESCE(played_at,NEW.closed_at) WHERE turn_id=NEW.turn_id;
END;
CREATE TABLE studio_audience (
    session TEXT NOT NULL REFERENCES studio_sessions(session), observed REAL NOT NULL,
    status TEXT NOT NULL, stream_id TEXT, stream_started REAL, viewers INTEGER CHECK(viewers>=0), PRIMARY KEY(session,observed)
);
CREATE TABLE studio_stream_summaries (
    platform TEXT NOT NULL, stream_id TEXT NOT NULL, started REAL NOT NULL, ended REAL NOT NULL,
    unique_viewers INTEGER NOT NULL CHECK(unique_viewers>=0),
    average_viewers REAL CHECK(average_viewers>=0), peak_viewers INTEGER CHECK(peak_viewers>=0),
    CHECK(ended>started), PRIMARY KEY(platform,stream_id)
);
