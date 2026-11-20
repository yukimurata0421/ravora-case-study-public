-- Additive: old memories/jobs and their dispatch history remain unchanged.
CREATE TABLE memory_episodes (
    memory_id TEXT PRIMARY KEY REFERENCES memory_items(memory_id),
    episode_id TEXT NOT NULL,
    subject_quote TEXT NOT NULL,
    event_state TEXT NOT NULL CHECK(event_state IN ('unknown','ongoing','waiting','resolved','ended')),
    state_quote TEXT NOT NULL DEFAULT '',
    time_quote TEXT NOT NULL DEFAULT '',
    previous_memory_id TEXT REFERENCES memory_items(memory_id),
    relation TEXT NOT NULL CHECK(relation IN ('new','update','correction','duplicate')),
    link_state TEXT NOT NULL CHECK(link_state IN ('new','proposed','confirmed')),
    interpretation TEXT NOT NULL DEFAULT 'model_interpretation' CHECK(interpretation='model_interpretation'),
    confirmed_at REAL);
CREATE INDEX episode_history ON memory_episodes(episode_id);
CREATE INDEX episode_previous ON memory_episodes(previous_memory_id,link_state);
CREATE INDEX memory_owner_recent ON memory_items(viewer_key,scope,created_at DESC);

CREATE TABLE semantic_retrievals (
    turn_id TEXT PRIMARY KEY REFERENCES conversation_turns(turn_id),
    call_id TEXT REFERENCES provider_calls(id),
    query_sha256 TEXT NOT NULL,
    candidate_ids TEXT NOT NULL CHECK(json_valid(candidate_ids)),
    decision TEXT NOT NULL CHECK(json_valid(decision)),
    used_for_generation INTEGER NOT NULL DEFAULT 0 CHECK(used_for_generation IN (0,1)),
    created_at REAL NOT NULL);

-- Derived text follows memory deletion/expiry, including source removal.
CREATE TRIGGER episode_memory_removed AFTER UPDATE OF deleted_at ON memory_items
WHEN new.deleted_at IS NOT NULL BEGIN
    UPDATE memory_episodes SET subject_quote='',state_quote='',time_quote='',event_state='unknown'
        WHERE memory_id=new.memory_id;
END;
