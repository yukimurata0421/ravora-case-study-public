-- Stable character facts and meaningful, source-backed experiences only.
-- Momentary emotion, desire, conflict and generation prompts never belong here.
CREATE TABLE character_profiles (
    character_id TEXT PRIMARY KEY CHECK(character_id='ray'),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
    traits TEXT NOT NULL CHECK(json_valid(traits)),
    updated_at REAL NOT NULL
);
INSERT INTO character_profiles VALUES('ray',1,
    '["curiosity","independent_opinion","friendly_competition","admits_mistakes","initiative","learns_from_relationships","knows_it_is_ai"]',
    unixepoch());

CREATE TABLE character_memories (
    memory_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope IN ('offline','cloud')),
    owner_hash TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK(kind IN ('preference','value','interest','goal','summary')),
    topic TEXT NOT NULL,
    strength REAL NOT NULL CHECK(strength BETWEEN 0 AND 1),
    importance REAL NOT NULL CHECK(importance BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted','resolved')),
    created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
    reference_count INTEGER NOT NULL DEFAULT 0, last_referenced_at REAL,
    UNIQUE(scope,owner_hash,kind,topic)
);
CREATE TABLE character_experiences (
    event_id TEXT NOT NULL REFERENCES source_events(id),
    turn_id TEXT NOT NULL REFERENCES conversation_turns(turn_id),
    memory_id TEXT NOT NULL REFERENCES character_memories(memory_id),
    owner_hash TEXT NOT NULL, day_jst TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('repeated_topic','shared_future_topic','expressed_preference')),
    contribution REAL NOT NULL CHECK(contribution BETWEEN 0 AND 0.020001),
    created_at REAL NOT NULL,
    PRIMARY KEY(event_id,memory_id),
    UNIQUE(owner_hash,day_jst,memory_id)
);
CREATE TABLE character_preference_history (
    id INTEGER PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES character_memories(memory_id),
    event_id TEXT REFERENCES source_events(id),
    previous_strength REAL NOT NULL, strength REAL NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('repeated_topic','shared_future_topic','expressed_preference','operator_update','source_removed','reset')),
    created_at REAL NOT NULL
);
CREATE INDEX character_recall ON character_memories(scope,owner_hash,status,importance,updated_at);
CREATE INDEX character_event ON character_experiences(event_id);

-- A removed viewer source withdraws its contribution; no copied quote survives.
CREATE TRIGGER character_source_removed AFTER UPDATE OF is_deleted ON source_events
WHEN new.is_deleted=1 AND old.is_deleted=0
BEGIN
    INSERT INTO character_preference_history(memory_id,event_id,previous_strength,strength,reason,created_at)
        SELECT m.memory_id,new.id,m.strength,max(0,m.strength-e.contribution),'source_removed',unixepoch()
        FROM character_memories m JOIN character_experiences e ON e.memory_id=m.memory_id WHERE e.event_id=new.id;
    UPDATE character_memories SET strength=max(0,strength-COALESCE(
        (SELECT contribution FROM character_experiences e WHERE e.event_id=new.id AND e.memory_id=character_memories.memory_id),0)),
        status=CASE WHEN kind IN ('goal','summary') OR (
            NOT EXISTS(SELECT 1 FROM character_experiences e WHERE e.memory_id=character_memories.memory_id AND e.event_id<>new.id)
            AND NOT EXISTS(SELECT 1 FROM character_preference_history h WHERE h.memory_id=character_memories.memory_id AND h.reason='operator_update')
        ) THEN 'deleted' ELSE status END
        WHERE memory_id IN (SELECT memory_id FROM character_experiences WHERE event_id=new.id);
    DELETE FROM character_experiences WHERE event_id=new.id;
END;
