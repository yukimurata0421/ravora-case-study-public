-- Local CPU work is not an external provider request. Preserve both ledgers.
ALTER TABLE developer_inputs ADD COLUMN local_attempted INTEGER NOT NULL DEFAULT 0 CHECK(local_attempted IN (0,1));
ALTER TABLE developer_inputs ADD COLUMN stt_diagnostics TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(stt_diagnostics));
