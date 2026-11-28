-- The local schedule is independent of the X account. Only the operator records an actual post.
CREATE TABLE twitch_schedule_promotions (
    scheduled_start INTEGER PRIMARY KEY,
    planned_at INTEGER,
    outcome TEXT NOT NULL CHECK(outcome IN ('planned','posted','skipped')),
    posted_at INTEGER,
    updated_at INTEGER NOT NULL,
    CHECK((outcome='posted') = (posted_at IS NOT NULL)),
    CHECK(planned_at IS NULL OR planned_at=scheduled_start+300),
    CHECK(outcome!='planned' OR planned_at IS NOT NULL),
    CHECK(posted_at IS NULL OR (posted_at>=scheduled_start AND posted_at<scheduled_start+7200))
);
