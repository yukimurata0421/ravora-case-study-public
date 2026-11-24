CREATE TABLE studio_speech_policies (
    session TEXT NOT NULL REFERENCES studio_sessions(session),
    first_frame INTEGER NOT NULL CHECK(first_frame >= 0),
    activated REAL NOT NULL,
    policy TEXT NOT NULL,
    PRIMARY KEY(session, first_frame)
);
