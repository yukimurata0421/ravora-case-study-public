-- Freeze the policy per input. Existing inputs retain their original single slot.
ALTER TABLE developer_inputs ADD COLUMN reaction_limit INTEGER NOT NULL DEFAULT 1 CHECK(reaction_limit IN (1,2));
ALTER TABLE developer_inputs ADD COLUMN reaction_applied_sequence INTEGER NOT NULL DEFAULT 0 CHECK(reaction_applied_sequence BETWEEN 0 AND 2);
ALTER TABLE developer_inputs ADD COLUMN reaction_result TEXT CHECK(reaction_result IS NULL OR json_valid(reaction_result));
CREATE TRIGGER reaction_limit_immutable BEFORE UPDATE OF reaction_limit ON developer_inputs
WHEN new.reaction_limit!=old.reaction_limit BEGIN
    SELECT RAISE(ABORT,'INPUT_REACTION_POLICY_IMMUTABLE');
END;

-- v8's shared claim remains an untouched first-claim/historical ledger.
-- A new two-stage input gets at most one reservation in each stage, even after
-- cancellation, failures, UNKNOWNs, another worker, or a process restart.
CREATE TABLE reaction_stage_claims (
    input_id TEXT NOT NULL REFERENCES developer_inputs(id),
    stage TEXT NOT NULL CHECK(stage IN ('partial','final')),
    call_id TEXT NOT NULL UNIQUE,claimed_at REAL NOT NULL,
    PRIMARY KEY(input_id,stage));

-- Erase the cached result with its raw source; keep ranks and consumed claims.
CREATE TRIGGER reaction_result_source_removed AFTER UPDATE OF is_deleted ON source_events
WHEN new.is_deleted=1 BEGIN
    UPDATE developer_inputs SET reaction_result=NULL WHERE source_event_id=new.id;
END;
CREATE TRIGGER reaction_result_source_purged AFTER UPDATE OF raw_purged_at ON source_events
WHEN new.raw_purged_at IS NOT NULL BEGIN
    UPDATE developer_inputs SET reaction_result=NULL WHERE source_event_id=new.id;
END;
