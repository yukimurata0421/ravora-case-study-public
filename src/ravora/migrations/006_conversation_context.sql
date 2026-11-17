-- Index existing replies in place; never duplicate their raw text or change old turns.
CREATE VIRTUAL TABLE conversation_fts USING fts5(comment,reply,content='conversation_turns',content_rowid='rowid',tokenize='unicode61');
INSERT INTO conversation_fts(conversation_fts) VALUES('rebuild');
CREATE TRIGGER conversation_fts_insert AFTER INSERT ON conversation_turns BEGIN
    INSERT INTO conversation_fts(rowid,comment,reply) VALUES(new.rowid,new.comment,new.reply);
END;
CREATE TRIGGER conversation_fts_update AFTER UPDATE OF comment,reply ON conversation_turns BEGIN
    INSERT INTO conversation_fts(conversation_fts,rowid,comment,reply) VALUES('delete',old.rowid,old.comment,old.reply);
    INSERT INTO conversation_fts(rowid,comment,reply) VALUES(new.rowid,new.comment,new.reply);
END;
CREATE TRIGGER conversation_fts_delete AFTER DELETE ON conversation_turns BEGIN
    INSERT INTO conversation_fts(conversation_fts,rowid,comment,reply) VALUES('delete',old.rowid,old.comment,old.reply);
END;

CREATE VIEW conversation_context AS
    SELECT t.rowid AS source_rowid,t.turn_id,t.source_event_id,s.viewer_key,s.platform,
           substr(t.viewer,1,instr(t.viewer,':')-1) AS scope,
           CASE WHEN s.platform IN ('developer','developer-local','developer-mock') THEN 1 ELSE 2 END AS tier,
           t.comment AS question,t.reply AS answer,t.closed_at,s.received_at
    FROM conversation_turns t JOIN source_events s ON s.id=t.source_event_id
    WHERE t.status='completed' AND t.played=1 AND t.reply!='' AND s.is_deleted=0
      AND s.raw_purged_at IS NULL AND s.expires_at>CAST(strftime('%s','now') AS REAL)
      AND t.viewer IN ('offline:'||s.viewer_key,'cloud:'||s.viewer_key)
    UNION ALL
    SELECT t.rowid,t.turn_id,t.source_event_id,s.viewer_key,s.platform,
           substr(t.viewer,1,instr(t.viewer,':')-1),3,'',t.reply,t.closed_at,s.received_at
    FROM conversation_turns t JOIN source_events s ON s.id=t.source_event_id
    WHERE t.status='completed' AND t.played=1 AND t.reply!='' AND s.is_deleted=0
      AND s.raw_purged_at IS NULL AND s.expires_at>CAST(strftime('%s','now') AS REAL)
      AND t.viewer IN ('offline:'||s.viewer_key,'cloud:'||s.viewer_key);

CREATE TABLE context_shares (
    id TEXT PRIMARY KEY,memory_id TEXT NOT NULL REFERENCES memory_items(memory_id),
    scope TEXT NOT NULL CHECK(scope IN ('offline','cloud')),owner_key TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,approved_at REAL NOT NULL,revoked_at REAL);
CREATE UNIQUE INDEX context_share_active ON context_shares(memory_id) WHERE revoked_at IS NULL;
CREATE INDEX context_share_owner ON context_shares(scope,owner_key,approved_at DESC);

CREATE TABLE context_retrievals (
    id INTEGER PRIMARY KEY,turn_id TEXT NOT NULL REFERENCES conversation_turns(turn_id),
    context_id TEXT NOT NULL,tier INTEGER NOT NULL CHECK(tier IN (1,2,3)),
    source_turn_id TEXT REFERENCES conversation_turns(turn_id),
    source_event_id TEXT NOT NULL REFERENCES source_events(id),
    memory_id TEXT REFERENCES memory_items(memory_id),share_id TEXT REFERENCES context_shares(id),
    position INTEGER NOT NULL CHECK(position>=0),context_chars INTEGER NOT NULL CHECK(context_chars>0),
    call_id TEXT REFERENCES provider_calls(id),context_prepared INTEGER NOT NULL CHECK(context_prepared IN (0,1)),
    selected_at REAL NOT NULL,UNIQUE(turn_id,context_id));
CREATE INDEX context_retrieval_call ON context_retrievals(call_id);
