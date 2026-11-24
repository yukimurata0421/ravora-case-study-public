-- One short-lived policy record per eligible receipt. The existing inbox owns retention.
CREATE TABLE chat_candidates (
    receipt TEXT PRIMARY KEY REFERENCES comment_inbox(receipt) ON DELETE CASCADE,
    waiting_since REAL NOT NULL, ticket TEXT NOT NULL, reply_parent TEXT,
    claimed_at REAL, opportunity TEXT, lane TEXT, reply_kind TEXT,
    parent_question TEXT, group_of TEXT, played_at REAL, asked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX chat_group ON chat_candidates(group_of) WHERE group_of IS NOT NULL;
CREATE INDEX chat_question_used ON chat_candidates(parent_question) WHERE parent_question IS NOT NULL;
CREATE INDEX chat_inbox_viewer ON comment_inbox(session, viewer, status);
ALTER TABLE studio_inputs ADD COLUMN selection_lane TEXT;
ALTER TABLE studio_inputs ADD COLUMN reply_kind TEXT;
ALTER TABLE studio_sessions ADD COLUMN selection_policy TEXT;
CREATE INDEX studio_reply_viewer ON studio_inputs(session, viewer, played_at) WHERE played_at IS NOT NULL;

-- An already running v23 studio can keep writing its original named columns during migration.
INSERT INTO chat_candidates(receipt,waiting_since,ticket)
    SELECT receipt,received,viewer FROM comment_inbox
    WHERE status='queued' AND source IN ('twitch','youtube','youtube-fixture');

CREATE TRIGGER chat_selection_recorded AFTER UPDATE OF claimed_at ON chat_candidates
WHEN NEW.claimed_at IS NOT NULL
BEGIN
    UPDATE studio_inputs SET selection_lane=NEW.lane,reply_kind=NEW.reply_kind WHERE receipt=NEW.receipt;
END;

CREATE TRIGGER chat_group_deleted BEFORE UPDATE OF played ON conversation_turns
WHEN OLD.played=0 AND NEW.played=1 AND EXISTS (
    SELECT 1 FROM chat_candidates c JOIN comment_inbox i USING(receipt)
    WHERE c.group_of=NEW.source_event_id AND i.status='removed')
BEGIN
    SELECT RAISE(IGNORE);
END;

CREATE TRIGGER chat_selection_played AFTER UPDATE OF played ON conversation_turns
WHEN OLD.played=0 AND NEW.played=1
BEGIN
    UPDATE chat_candidates SET played_at=NEW.closed_at
        WHERE receipt=NEW.source_event_id;
    UPDATE comment_inbox SET status='covered',reason='group_answer'
        WHERE status='queued' AND receipt IN
            (SELECT receipt FROM chat_candidates WHERE group_of=NEW.source_event_id);
END;

CREATE TRIGGER chat_group_released AFTER UPDATE OF status ON comment_inbox
WHEN NEW.status IN ('removed','cancelled','expired','failed','held') AND OLD.status!=NEW.status
BEGIN
    UPDATE comment_inbox SET status='dropped',reason='viewer_updated'
        WHERE receipt IN (SELECT receipt FROM chat_candidates WHERE group_of=NEW.receipt)
          AND status='queued' AND EXISTS (SELECT 1 FROM comment_inbox newer
              JOIN chat_candidates c ON c.receipt=newer.receipt
              WHERE newer.session=NEW.session AND newer.viewer=comment_inbox.viewer AND newer.status='queued'
                AND c.group_of IS NULL AND newer.receipt!=comment_inbox.receipt);
    UPDATE chat_candidates SET group_of=NULL WHERE group_of=NEW.receipt;
END;
