-- Keep the local policy and all existing jobs. Bind remote work to its model
-- and durable dispatch record so paid/unknown requests are never auto-retried.
ALTER TABLE memory_jobs ADD COLUMN model TEXT;
ALTER TABLE memory_jobs ADD COLUMN min_confidence REAL NOT NULL DEFAULT 0.7 CHECK(min_confidence>=0 AND min_confidence<=1);
ALTER TABLE memory_jobs ADD COLUMN api_call_id TEXT REFERENCES provider_calls(id);
ALTER TABLE memory_jobs ADD COLUMN request_attempted INTEGER NOT NULL DEFAULT 0 CHECK(request_attempted IN (0,1));
ALTER TABLE memory_jobs ADD COLUMN request_state TEXT NOT NULL DEFAULT 'not_requested';
ALTER TABLE memory_sources ADD COLUMN quote_start INTEGER;
ALTER TABLE memory_sources ADD COLUMN quote_end INTEGER;
ALTER TABLE memory_sources ADD COLUMN quote_sha256 TEXT;
CREATE INDEX memory_job_policy ON memory_jobs(scope,policy,status,enqueued_at);
