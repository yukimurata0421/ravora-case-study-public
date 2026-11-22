CREATE TABLE dialogue_hooks (
    scope TEXT NOT NULL CHECK(scope IN ('offline','cloud')),
    owner_hash TEXT NOT NULL,
    hook TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES source_events(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'unspoken' CHECK(status IN ('unspoken','used')),
    created_at REAL NOT NULL,
    claimed_turn TEXT,
    claim_until REAL,
    PRIMARY KEY(scope,owner_hash,hook)
);
CREATE INDEX dialogue_hooks_source ON dialogue_hooks(source_id);
CREATE UNIQUE INDEX dialogue_hooks_claim ON dialogue_hooks(claimed_turn) WHERE claimed_turn IS NOT NULL;
CREATE TRIGGER expire_dialogue_hooks AFTER UPDATE OF is_deleted,raw_purged_at ON source_events
WHEN NEW.is_deleted=1 OR NEW.raw_purged_at IS NOT NULL
BEGIN
    DELETE FROM dialogue_hooks WHERE source_id=NEW.id;
END;
