CREATE TABLE youtube_chat_connections (
    target TEXT NOT NULL,
    reserved_at REAL NOT NULL
);
CREATE INDEX youtube_chat_connections_window ON youtube_chat_connections(target, reserved_at);
