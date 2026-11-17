-- Provisional STT predictions precede a conversation turn. Keep their attempts
-- separate from final transcripts, spoken answers, and retrievable memories.
CREATE TABLE early_reaction_calls (
    id TEXT PRIMARY KEY,
    input_id TEXT NOT NULL REFERENCES developer_inputs(id),
    turn_id TEXT REFERENCES conversation_turns(turn_id),
    sequence INTEGER NOT NULL CHECK(sequence>0),
    model TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('mock','cloud')),
    text_sha256 TEXT NOT NULL, characters INTEGER NOT NULL CHECK(characters>0),
    state TEXT NOT NULL, attempted INTEGER NOT NULL DEFAULT 0 CHECK(attempted IN (0,1)),
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    latency_ms REAL, emitted_at REAL, reaction TEXT, code TEXT,
    usage TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(usage)),
    UNIQUE(input_id,sequence));
CREATE INDEX early_reaction_input ON early_reaction_calls(input_id,sequence);
