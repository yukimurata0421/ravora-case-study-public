-- Store the observed identity on its source. A name is never an identity key.
ALTER TABLE relationship_events ADD COLUMN session TEXT;
ALTER TABLE relationship_events ADD COLUMN display_name TEXT;
ALTER TABLE relationship_events ADD COLUMN preferred_name TEXT;
ALTER TABLE relationship_events ADD COLUMN name_missing INTEGER NOT NULL DEFAULT 0 CHECK(name_missing IN (0,1));
CREATE INDEX relationship_prior ON relationship_events(scope,viewer_key,observed_at) WHERE eligible=1;
CREATE INDEX relationship_display ON relationship_events(scope,display_name COLLATE NOCASE) WHERE display_name IS NOT NULL;
CREATE INDEX relationship_preferred ON relationship_events(scope,preferred_name COLLATE NOCASE) WHERE preferred_name IS NOT NULL;
CREATE TABLE viewer_name_optouts (
    scope TEXT NOT NULL, viewer_key TEXT NOT NULL, PRIMARY KEY(scope,viewer_key)
);
UPDATE relationship_events AS r SET
    session=coalesce((SELECT session FROM studio_inputs WHERE receipt=r.source_event_id),
                     (SELECT session FROM comment_inbox WHERE receipt=r.source_event_id)),
    display_name=(SELECT nullif(nullif(i.display_name,''),'Viewer') FROM comment_inbox i
                  JOIN source_events s ON s.id=i.receipt WHERE i.receipt=r.source_event_id
                  AND r.eligible=1 AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>unixepoch());
CREATE TRIGGER viewer_name_source_removed AFTER UPDATE OF is_deleted ON source_events
WHEN new.is_deleted=1 BEGIN
    UPDATE relationship_events SET display_name=NULL,preferred_name=NULL,name_missing=0 WHERE source_event_id=new.id;
END;
CREATE TRIGGER viewer_name_raw_expired AFTER UPDATE OF raw_purged_at ON source_events
WHEN new.raw_purged_at IS NOT NULL BEGIN
    UPDATE relationship_events SET display_name=NULL,name_missing=0 WHERE source_event_id=new.id;
END;
-- Withdraw legacy frequency-only contributions from global taste. Preserve the audit records.
UPDATE character_memories AS m SET
    strength=max(0,strength-coalesce((SELECT sum(contribution) FROM character_experiences
                 WHERE memory_id=m.memory_id AND reason='repeated_topic'),0)),
    status=CASE WHEN EXISTS(SELECT 1 FROM character_preference_history
                 WHERE memory_id=m.memory_id AND reason='operator_update') THEN status ELSE 'resolved' END
    WHERE owner_hash='' AND kind='interest' AND status='active' AND EXISTS
      (SELECT 1 FROM character_experiences WHERE memory_id=m.memory_id AND reason='repeated_topic');
UPDATE character_experiences SET contribution=0 WHERE reason='repeated_topic' AND memory_id IN
    (SELECT memory_id FROM character_memories WHERE owner_hash='' AND kind='interest');
