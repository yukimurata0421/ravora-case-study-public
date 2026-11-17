-- One durable Jev reservation shared by partial STT and final-text fallback.
CREATE TABLE input_reaction_claims (
    input_id TEXT PRIMARY KEY REFERENCES developer_inputs(id),
    stage TEXT NOT NULL CHECK(stage IN ('partial','final','historical')),
    call_id TEXT, claimed_at REAL NOT NULL);

-- Preserve previous attempts and UNKNOWNs: upgrading must not restore a slot.
INSERT INTO input_reaction_claims(input_id,stage,claimed_at)
SELECT input_id,'historical',MIN(created_at) FROM (
    SELECT input_id,created_at FROM early_reaction_calls
    UNION ALL
    SELECT d.id,p.prepared FROM developer_inputs d
    JOIN conversation_turns t ON t.source_event_id=d.source_event_id
    JOIN provider_calls p ON p.turn_id=t.turn_id WHERE p.provider='jev'
) GROUP BY input_id;
