-- Confirmed playout and extraction are connected by a durable, network-free queue.
CREATE TABLE memory_jobs (
    turn_id TEXT PRIMARY KEY REFERENCES conversation_turns(turn_id),
    source_event_id TEXT NOT NULL REFERENCES source_events(id),
    viewer_key TEXT NOT NULL, scope TEXT NOT NULL CHECK(scope IN ('offline','cloud')),
    policy TEXT NOT NULL, ttl_seconds INTEGER NOT NULL CHECK(ttl_seconds>0),
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','skipped','failed','cancelled')),
    enqueued_at REAL NOT NULL, claimed_at REAL, completed_at REAL,
    claim_token TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT, candidate_count INTEGER NOT NULL DEFAULT 0);
CREATE INDEX memory_job_queue ON memory_jobs(scope,status,enqueued_at);
CREATE INDEX memory_job_viewer ON memory_jobs(viewer_key,scope,status);
CREATE TRIGGER memory_job_played BEFORE INSERT ON memory_jobs
WHEN NOT EXISTS(SELECT 1 FROM conversation_turns t JOIN source_events s ON s.id=t.source_event_id
    WHERE t.turn_id=new.turn_id AND t.played=1 AND t.status='completed'
      AND s.id=new.source_event_id AND s.viewer_key=new.viewer_key
      AND t.viewer=new.scope||':'||s.viewer_key AND s.is_deleted=0)
BEGIN SELECT RAISE(ABORT,'MEMORY_REQUIRES_MATCHING_PLAYED_SOURCE'); END;
ALTER TABLE memory_items ADD COLUMN extraction_turn_id TEXT REFERENCES memory_jobs(turn_id);
ALTER TABLE memory_items ADD COLUMN topic_key TEXT;
ALTER TABLE memory_items ADD COLUMN polarity TEXT CHECK(polarity IN ('like','dislike'));
ALTER TABLE memory_items ADD COLUMN approval_method TEXT;
CREATE UNIQUE INDEX memory_extraction_once ON memory_items(extraction_turn_id,topic_key)
    WHERE extraction_turn_id IS NOT NULL;
CREATE INDEX memory_current_topic ON memory_items(viewer_key,scope,topic_key,created_at);

CREATE TABLE memory_retrievals (
    turn_id TEXT NOT NULL REFERENCES conversation_turns(turn_id),
    memory_id TEXT NOT NULL REFERENCES memory_items(memory_id),
    selected_at REAL NOT NULL, context_prepared INTEGER NOT NULL CHECK(context_prepared IN (0,1)),
    strategy TEXT NOT NULL, call_id TEXT REFERENCES provider_calls(id),
    PRIMARY KEY(turn_id,memory_id));

CREATE TRIGGER memory_job_source_removed AFTER UPDATE OF is_deleted ON source_events
WHEN old.is_deleted=0 AND new.is_deleted=1 BEGIN
    UPDATE memory_jobs SET status='cancelled',reason='SOURCE_REMOVED',completed_at=new.deleted_at,
        claim_token=NULL,lease_until=NULL
        WHERE source_event_id=new.id AND status IN ('pending','running');
END;
