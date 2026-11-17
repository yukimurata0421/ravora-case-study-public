-- STT happens before a conversation turn exists. Keep its request/usage and
-- cancellation history even when no transcript is accepted by the LLM.
CREATE TABLE developer_inputs (
    id TEXT PRIMARY KEY, session TEXT NOT NULL, client_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('audio','text')),
    payload_sha256 TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('mock','cloud')),
    created_at REAL NOT NULL, updated_at REAL NOT NULL, month TEXT NOT NULL,
    input_state TEXT NOT NULL, code TEXT,
    duration_seconds REAL, provider TEXT, model TEXT, response_model TEXT,
    request_attempted INTEGER NOT NULL DEFAULT 0 CHECK(request_attempted IN (0,1)),
    input_tokens INTEGER, output_tokens INTEGER, thought_tokens INTEGER, total_tokens INTEGER,
    latency_ms REAL, cost_estimate REAL,
    language TEXT, source_event_id TEXT REFERENCES source_events(id),
    UNIQUE(session,client_id));
CREATE INDEX developer_inputs_session ON developer_inputs(session,created_at);
CREATE INDEX developer_stt_month ON developer_inputs(month,request_attempted);
