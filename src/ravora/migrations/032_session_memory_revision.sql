-- Invalidate even paraphrased short-term replies when another writer revokes evidence.
CREATE TABLE session_evidence_revision (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
INSERT INTO session_evidence_revision VALUES (1,0);
CREATE TRIGGER session_source_changed AFTER UPDATE OF text,is_deleted,raw_purged_at,expires_at,viewer_key,stream_id ON source_events
WHEN OLD.text IS NOT NEW.text OR OLD.is_deleted IS NOT NEW.is_deleted OR OLD.raw_purged_at IS NOT NEW.raw_purged_at
  OR OLD.expires_at IS NOT NEW.expires_at OR OLD.viewer_key IS NOT NEW.viewer_key OR OLD.stream_id IS NOT NEW.stream_id
BEGIN UPDATE session_evidence_revision SET version=version+1 WHERE id=1; END;
CREATE TRIGGER session_source_gone AFTER DELETE ON source_events
BEGIN UPDATE session_evidence_revision SET version=version+1 WHERE id=1; END;
CREATE TRIGGER session_approved_note_changed AFTER UPDATE ON memory_items
WHEN OLD.review_state='approved' AND (OLD.review_state IS NOT NEW.review_state OR OLD.deleted_at IS NOT NEW.deleted_at
 OR OLD.summary IS NOT NEW.summary OR OLD.expires_at IS NOT NEW.expires_at OR OLD.viewer_key IS NOT NEW.viewer_key
 OR OLD.scope IS NOT NEW.scope OR OLD.sensitivity IS NOT NEW.sensitivity)
BEGIN UPDATE session_evidence_revision SET version=version+1 WHERE id=1; END;
CREATE TRIGGER session_note_gone AFTER DELETE ON memory_items WHEN OLD.review_state='approved'
BEGIN UPDATE session_evidence_revision SET version=version+1 WHERE id=1; END;
CREATE TRIGGER session_spoken_edit AFTER UPDATE OF played,comment,reply,status ON conversation_turns
WHEN OLD.played=1 AND (OLD.played IS NOT NEW.played OR OLD.comment IS NOT NEW.comment OR OLD.reply IS NOT NEW.reply OR OLD.status IS NOT NEW.status)
BEGIN UPDATE session_evidence_revision SET version=version+1 WHERE id=1; END;
CREATE TRIGGER session_spoken_gone AFTER DELETE ON conversation_turns WHEN OLD.played=1
BEGIN UPDATE session_evidence_revision SET version=version+1 WHERE id=1; END;
