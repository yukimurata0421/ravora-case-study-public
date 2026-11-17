-- Add the design's logical records without replacing the prototype's ledgers.
CREATE TABLE IF NOT EXISTS conversation_turns (
    id TEXT PRIMARY KEY, viewer TEXT NOT NULL, comment TEXT NOT NULL,
    reply TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
    played INTEGER NOT NULL DEFAULT 0, started TEXT NOT NULL,
    metrics TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS speech_usage (
    id TEXT PRIMARY KEY, month TEXT NOT NULL, characters INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_calls (
    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, provider TEXT NOT NULL, operation TEXT NOT NULL,
    state TEXT NOT NULL, attempted INTEGER NOT NULL DEFAULT 0, prepared REAL NOT NULL,
    updated REAL NOT NULL, summary TEXT NOT NULL, code TEXT);
CREATE TABLE IF NOT EXISTS comment_inbox (
    receipt TEXT PRIMARY KEY, session TEXT NOT NULL, source TEXT NOT NULL, stream_id TEXT NOT NULL,
    message_id TEXT NOT NULL, viewer TEXT NOT NULL, display_name TEXT NOT NULL, text TEXT NOT NULL,
    published REAL NOT NULL, received REAL NOT NULL, expires REAL NOT NULL,
    status TEXT NOT NULL, reason TEXT, turn_id TEXT);
CREATE INDEX IF NOT EXISTS comment_queue ON comment_inbox(session,status,received);
CREATE TABLE IF NOT EXISTS youtube_cursors (
    chat_id TEXT PRIMARY KEY, token TEXT NOT NULL, updated REAL NOT NULL);

CREATE TABLE source_events (
    id TEXT PRIMARY KEY, platform TEXT NOT NULL, external_event_id TEXT NOT NULL,
    stream_id TEXT NOT NULL, viewer_id TEXT, viewer_key TEXT NOT NULL,
    text TEXT NOT NULL, published_at REAL, received_at REAL NOT NULL,
    expires_at REAL NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0,1)),
    deleted_at REAL, raw_purged_at REAL,
    UNIQUE(platform,stream_id,external_event_id));
CREATE INDEX source_viewer_time ON source_events(viewer_key,received_at DESC);
CREATE INDEX source_expiry ON source_events(expires_at) WHERE raw_purged_at IS NULL;
INSERT INTO source_events(id,platform,external_event_id,stream_id,viewer_key,text,
                          published_at,received_at,expires_at,is_deleted)
    SELECT receipt,source,message_id,stream_id,viewer,text,published,received,received+86400,
           CASE WHEN status='removed' THEN 1 ELSE 0 END FROM comment_inbox;

ALTER TABLE conversation_turns ADD COLUMN turn_id TEXT;
ALTER TABLE conversation_turns ADD COLUMN source_event_id TEXT REFERENCES source_events(id);
ALTER TABLE conversation_turns ADD COLUMN state TEXT;
ALTER TABLE conversation_turns ADD COLUMN reaction TEXT;
ALTER TABLE conversation_turns ADD COLUMN created_at REAL;
ALTER TABLE conversation_turns ADD COLUMN closed_at REAL;
CREATE UNIQUE INDEX conversation_turn_identity ON conversation_turns(turn_id);
CREATE INDEX conversation_source ON conversation_turns(source_event_id);
CREATE INDEX conversation_viewer_time ON conversation_turns(viewer,started DESC);
UPDATE conversation_turns SET turn_id=id,
    source_event_id=(SELECT receipt FROM comment_inbox WHERE turn_id=conversation_turns.id LIMIT 1),
    state=CASE WHEN played=1 AND status='completed' THEN 'COMPLETED'
               WHEN status='completed' THEN 'GENERATED' ELSE upper(status) END,
    created_at=(julianday(started)-2440587.5)*86400.0;

CREATE TABLE utterances (
    utterance_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES conversation_turns(turn_id),
    chunk_index INTEGER NOT NULL CHECK(chunk_index>=0), text TEXT NOT NULL,
    audio_path TEXT, audio_sha256 TEXT, duration_seconds REAL CHECK(duration_seconds>0),
    status TEXT NOT NULL, created_at REAL NOT NULL,
    started_at REAL, ended_at REAL, playback_ack_at REAL,
    UNIQUE(turn_id,chunk_index));
CREATE INDEX utterance_turn ON utterances(turn_id,chunk_index);

CREATE TABLE memory_items (
    memory_id TEXT PRIMARY KEY, viewer_key TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('offline','cloud')),
    summary TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags) AND json_type(tags)='array'),
    confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
    claim_kind TEXT NOT NULL DEFAULT 'viewer_report' CHECK(claim_kind='viewer_report'),
    review_state TEXT NOT NULL DEFAULT 'candidate' CHECK(review_state IN ('candidate','approved','deleted')),
    sensitivity TEXT NOT NULL DEFAULT 'unreviewed' CHECK(sensitivity IN ('unreviewed','non_sensitive')),
    created_at REAL NOT NULL, expires_at REAL NOT NULL CHECK(expires_at>created_at),
    deleted_at REAL, deletion_reason TEXT,
    CHECK(review_state!='approved' OR sensitivity='non_sensitive'));
CREATE INDEX memory_viewer_expiry ON memory_items(viewer_key,scope,expires_at);
CREATE TABLE memory_sources (
    memory_id TEXT NOT NULL REFERENCES memory_items(memory_id),
    event_id TEXT NOT NULL REFERENCES source_events(id),
    source_type TEXT NOT NULL DEFAULT 'viewer_report' CHECK(source_type='viewer_report'),
    PRIMARY KEY(memory_id,event_id));
CREATE INDEX memory_source_event ON memory_sources(event_id,memory_id);
CREATE TRIGGER memory_source_viewer BEFORE INSERT ON memory_sources
WHEN (SELECT viewer_key FROM source_events WHERE id=new.event_id)
       != (SELECT viewer_key FROM memory_items WHERE memory_id=new.memory_id)
     OR (SELECT is_deleted FROM source_events WHERE id=new.event_id)=1
BEGIN SELECT RAISE(ABORT,'MEMORY_SOURCE_VIEWER_OR_DELETION_MISMATCH'); END;

CREATE VIRTUAL TABLE memory_fts USING fts5(summary,tags,content='memory_items',content_rowid='rowid',tokenize='unicode61');
CREATE TRIGGER memory_fts_insert AFTER INSERT ON memory_items BEGIN
    INSERT INTO memory_fts(rowid,summary,tags) VALUES(new.rowid,new.summary,new.tags);
END;
CREATE TRIGGER memory_fts_delete AFTER DELETE ON memory_items BEGIN
    INSERT INTO memory_fts(memory_fts,rowid,summary,tags) VALUES('delete',old.rowid,old.summary,old.tags);
END;
CREATE TRIGGER memory_fts_update AFTER UPDATE OF summary,tags ON memory_items BEGIN
    INSERT INTO memory_fts(memory_fts,rowid,summary,tags) VALUES('delete',old.rowid,old.summary,old.tags);
    INSERT INTO memory_fts(rowid,summary,tags) VALUES(new.rowid,new.summary,new.tags);
END;

CREATE TABLE action_events (
    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES conversation_turns(turn_id),
    action TEXT NOT NULL, requested_at REAL NOT NULL,
    rendered_at REAL, playout_at REAL,
    planned_playout_at REAL, clock_domain TEXT,
    details TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details)));
CREATE INDEX action_turn_time ON action_events(turn_id,requested_at);

CREATE TABLE api_usage (
    id TEXT PRIMARY KEY REFERENCES provider_calls(id),
    turn_id TEXT NOT NULL REFERENCES conversation_turns(turn_id),
    provider TEXT NOT NULL, model TEXT, response_model TEXT, mode TEXT NOT NULL,
    input_tokens INTEGER CHECK(input_tokens>=0), output_tokens INTEGER CHECK(output_tokens>=0),
    total_tokens INTEGER CHECK(total_tokens>=0), thought_tokens INTEGER CHECK(thought_tokens>=0),
    cached_input_tokens INTEGER CHECK(cached_input_tokens>=0),
    latency_ms REAL CHECK(latency_ms>=0), cost_estimate REAL CHECK(cost_estimate>=0),
    characters INTEGER CHECK(characters>=0), attempted INTEGER NOT NULL DEFAULT 0 CHECK(attempted IN (0,1)),
    status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX api_usage_time ON api_usage(provider,created_at);
INSERT INTO api_usage(id,turn_id,provider,mode,characters,attempted,status,created_at)
    SELECT p.id,p.turn_id,p.provider,
           COALESCE(json_extract(t.metrics,'$.mode'),'unknown'),
           json_extract(p.summary,'$.characters'),p.attempted,p.state,p.prepared
      FROM provider_calls p JOIN conversation_turns t ON t.turn_id=p.turn_id;

-- Logical deletion preserves event identity and control history, but removes content.
CREATE TRIGGER source_event_removed AFTER UPDATE OF is_deleted ON source_events
WHEN old.is_deleted=0 AND new.is_deleted=1 BEGIN
    UPDATE source_events SET text='',viewer_id=NULL,raw_purged_at=new.deleted_at WHERE id=new.id;
    UPDATE conversation_turns SET comment='',reply='',played=0,status='source_removed',
        state='CANCELLED',reaction=NULL,closed_at=COALESCE(closed_at,new.deleted_at)
        WHERE source_event_id=new.id;
    UPDATE utterances SET text='',audio_path=NULL,audio_sha256=NULL,status='source_removed'
        WHERE turn_id IN (SELECT turn_id FROM conversation_turns WHERE source_event_id=new.id);
    UPDATE memory_items SET summary='',tags='[]',review_state='deleted',deleted_at=new.deleted_at,
        deletion_reason='source_removed'
        WHERE memory_id IN (SELECT memory_id FROM memory_sources WHERE event_id=new.id);
END;
