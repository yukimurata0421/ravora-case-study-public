-- Contact statistics are separate from source-backed factual memories.
-- No old comments are backfilled: their historical classification is unknown.
CREATE TABLE viewer_relationships (
    scope TEXT NOT NULL CHECK(scope IN ('offline','cloud')), viewer_key TEXT NOT NULL,
    posted_comments INTEGER NOT NULL DEFAULT 0 CHECK(posted_comments>=0),
    eligible_comments INTEGER NOT NULL DEFAULT 0 CHECK(eligible_comments>=0),
    played_exchanges INTEGER NOT NULL DEFAULT 0 CHECK(played_exchanges>=0),
    eligible_exchanges INTEGER NOT NULL DEFAULT 0 CHECK(eligible_exchanges>=0),
    active_days INTEGER NOT NULL DEFAULT 0 CHECK(active_days>=0),
    familiarity_units INTEGER NOT NULL DEFAULT 0 CHECK(familiarity_units>=0),
    PRIMARY KEY(scope,viewer_key));

CREATE TABLE relationship_days (
    scope TEXT NOT NULL CHECK(scope IN ('offline','cloud')), viewer_key TEXT NOT NULL,
    day INTEGER NOT NULL, -- Gregorian ordinal in Asia/Tokyo, based on first receipt.
    posted_comments INTEGER NOT NULL CHECK(posted_comments>=0),
    eligible_comments INTEGER NOT NULL CHECK(eligible_comments>=0),
    played_exchanges INTEGER NOT NULL CHECK(played_exchanges>=0),
    eligible_exchanges INTEGER NOT NULL CHECK(eligible_exchanges>=0),
    PRIMARY KEY(scope,viewer_key,day));
CREATE INDEX relationship_active_days ON relationship_days(scope,viewer_key,day DESC) WHERE eligible_comments>0;

CREATE TABLE relationship_events (
    source_event_id TEXT PRIMARY KEY REFERENCES source_events(id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK(scope IN ('offline','cloud')), viewer_key TEXT NOT NULL,
    day INTEGER NOT NULL, observed_at REAL NOT NULL,
    fresh INTEGER NOT NULL CHECK(fresh IN (0,1)),
    eligible INTEGER NOT NULL CHECK(eligible IN (0,1) AND eligible<=fresh),
    reason TEXT NOT NULL CHECK(reason IN ('eligible','duplicate_content','promotion','spam','unrelated',
        'historical','future_timestamp','opt_out','source_removed')),
    assessment TEXT NOT NULL CHECK(assessment IN ('local-v1','moderator','semantic')),
    fingerprint TEXT, context_turn_id TEXT REFERENCES conversation_turns(turn_id),
    played_turn_id TEXT UNIQUE REFERENCES conversation_turns(turn_id), played_at REAL,
    policy TEXT NOT NULL CHECK(policy='contact-v1'),
    CHECK((played_turn_id IS NULL AND played_at IS NULL) OR
          (played_turn_id IS NOT NULL AND played_at IS NOT NULL)));
CREATE INDEX relationship_repeat ON relationship_events(scope,viewer_key,fingerprint,observed_at DESC);
CREATE INDEX relationship_played ON relationship_events(scope,viewer_key,played_at DESC) WHERE played_turn_id IS NOT NULL;

-- Daily caps: three eligible posts plus two acknowledged exchanges. Raw counts
-- stay exact; excess posts never imply distrust or an additional relationship gain.
CREATE TRIGGER relationship_day_insert AFTER INSERT ON relationship_days BEGIN
    INSERT INTO viewer_relationships VALUES(new.scope,new.viewer_key,new.posted_comments,
        new.eligible_comments,new.played_exchanges,new.eligible_exchanges,CAST(new.eligible_comments>0 AS INTEGER),
        min(new.eligible_comments,3)+min(new.eligible_exchanges,2))
    ON CONFLICT(scope,viewer_key) DO UPDATE SET
        posted_comments=posted_comments+excluded.posted_comments,
        eligible_comments=eligible_comments+excluded.eligible_comments,
        played_exchanges=played_exchanges+excluded.played_exchanges,
        eligible_exchanges=eligible_exchanges+excluded.eligible_exchanges,
        active_days=active_days+excluded.active_days,
        familiarity_units=familiarity_units+excluded.familiarity_units;
END;
CREATE TRIGGER relationship_day_update AFTER UPDATE ON relationship_days BEGIN
    UPDATE viewer_relationships SET
        posted_comments=posted_comments+new.posted_comments-old.posted_comments,
        eligible_comments=eligible_comments+new.eligible_comments-old.eligible_comments,
        played_exchanges=played_exchanges+new.played_exchanges-old.played_exchanges,
        eligible_exchanges=eligible_exchanges+new.eligible_exchanges-old.eligible_exchanges,
        active_days=active_days+(new.eligible_comments>0)-(old.eligible_comments>0),
        familiarity_units=familiarity_units+min(new.eligible_comments,3)-min(old.eligible_comments,3)
            +min(new.eligible_exchanges,2)-min(old.eligible_exchanges,2)
    WHERE scope=new.scope AND viewer_key=new.viewer_key;
END;
CREATE TRIGGER relationship_event_insert AFTER INSERT ON relationship_events BEGIN
    INSERT INTO relationship_days VALUES(new.scope,new.viewer_key,new.day,1,new.eligible,
        CAST(new.played_turn_id IS NOT NULL AS INTEGER),new.eligible*(new.played_turn_id IS NOT NULL))
    ON CONFLICT(scope,viewer_key,day) DO UPDATE SET
        posted_comments=posted_comments+1,eligible_comments=eligible_comments+excluded.eligible_comments,
        played_exchanges=played_exchanges+excluded.played_exchanges,
        eligible_exchanges=eligible_exchanges+excluded.eligible_exchanges;
END;
CREATE TRIGGER relationship_event_update AFTER UPDATE OF eligible,played_turn_id ON relationship_events
WHEN new.eligible!=old.eligible OR new.played_turn_id IS NOT old.played_turn_id BEGIN
    UPDATE relationship_days SET eligible_comments=eligible_comments+new.eligible-old.eligible,
        played_exchanges=played_exchanges+(new.played_turn_id IS NOT NULL)-(old.played_turn_id IS NOT NULL),
        eligible_exchanges=eligible_exchanges+new.eligible*(new.played_turn_id IS NOT NULL)-old.eligible*(old.played_turn_id IS NOT NULL)
    WHERE scope=new.scope AND viewer_key=new.viewer_key AND day=new.day;
END;
CREATE TRIGGER relationship_event_delete AFTER DELETE ON relationship_events BEGIN
    UPDATE relationship_days SET posted_comments=posted_comments-1,eligible_comments=eligible_comments-old.eligible,
        played_exchanges=played_exchanges-(old.played_turn_id IS NOT NULL),
        eligible_exchanges=eligible_exchanges-old.eligible*(old.played_turn_id IS NOT NULL)
    WHERE scope=old.scope AND viewer_key=old.viewer_key AND day=old.day;
END;
CREATE TRIGGER relationship_source_removed AFTER UPDATE OF is_deleted ON source_events
WHEN new.is_deleted=1 AND old.is_deleted=0 BEGIN
    UPDATE relationship_events SET eligible=0,reason='source_removed',fingerprint=NULL,
        context_turn_id=NULL,played_turn_id=NULL,played_at=NULL WHERE source_event_id=new.id;
END;
CREATE TRIGGER relationship_raw_purged AFTER UPDATE OF raw_purged_at ON source_events
WHEN new.raw_purged_at IS NOT NULL BEGIN
    UPDATE relationship_events SET fingerprint=NULL,context_turn_id=NULL WHERE source_event_id=new.id;
END;
