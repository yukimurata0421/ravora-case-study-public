-- Ranked owner lookup for semantic memory retrieval (src/ravora/memory.py uses INDEXED BY).
-- Split from 011, which was already applied before this index was added to it.
CREATE INDEX memory_owner_rank ON memory_items(viewer_key,scope,confidence DESC,created_at DESC)
    WHERE deleted_at IS NULL;
