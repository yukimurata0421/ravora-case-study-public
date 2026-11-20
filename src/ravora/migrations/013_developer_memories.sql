-- Attribute developer inputs without duplicating transcripts or extracted memories.
CREATE TABLE developer_memory_sources (
    event_id TEXT PRIMARY KEY NOT NULL REFERENCES source_events(id) ON DELETE CASCADE,
    owner_key TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX developer_memory_sources_owner ON developer_memory_sources(owner_key);

INSERT INTO developer_memory_sources(event_id,owner_key)
    SELECT id,viewer_key FROM source_events
    WHERE platform IN ('developer','developer-local','developer-mock')
      AND viewer_key GLOB (platform||':v2:*') AND is_deleted=0;

CREATE TRIGGER developer_source_added AFTER INSERT ON source_events
WHEN new.platform IN ('developer','developer-local','developer-mock')
  AND new.viewer_key GLOB (new.platform||':v2:*') AND new.is_deleted=0 BEGIN
    INSERT INTO developer_memory_sources(event_id,owner_key) VALUES(new.id,new.viewer_key);
END;
CREATE TRIGGER developer_source_removed AFTER UPDATE OF is_deleted ON source_events
WHEN new.is_deleted=1 BEGIN
    DELETE FROM developer_memory_sources WHERE event_id=new.id;
END;
