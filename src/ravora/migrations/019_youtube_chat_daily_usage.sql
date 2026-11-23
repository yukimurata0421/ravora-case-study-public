CREATE TABLE youtube_chat_daily_usage (
    period_start INTEGER PRIMARY KEY,
    reserved_units INTEGER NOT NULL CHECK (reserved_units >= 0)
);
