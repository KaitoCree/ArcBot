CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE users (
    discord_id               INTEGER PRIMARY KEY,
    ingame_name              TEXT,
    points                   INTEGER NOT NULL DEFAULT 0,
    rank_key                 TEXT,              -- NULL until placed
    provisional              INTEGER NOT NULL DEFAULT 0,  -- 1 while a pending claim holds a provisional rank
    veteran_granted          INTEGER NOT NULL DEFAULT 0,
    placed_at                TEXT,
    last_activity_at         TEXT,
    last_stats_submission_at TEXT,
    created_at               TEXT NOT NULL
);

-- Append-only audit log. Never UPDATE or DELETE rows; add compensating rows instead.
CREATE TABLE point_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    source     TEXT NOT NULL,     -- stat_seed | vouch | job | mod_award | revoke | import
    ref        TEXT,
    actor_id   INTEGER,
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_point_events_user ON point_events(discord_id, created_at);

CREATE TABLE stats_submissions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id        INTEGER NOT NULL,
    kind              TEXT NOT NULL,   -- onboarding | promotion
    source            TEXT NOT NULL,   -- ocr | manual | mixed
    ingame_name       TEXT,
    hours             REAL,
    knockouts         INTEGER,
    squad_revives     INTEGER,
    stranger_revives  INTEGER,
    quests            INTEGER,
    containers        INTEGER,
    expeditions       INTEGER,
    rating            REAL,
    assessed_rank     TEXT,
    flags             TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL,   -- auto_placed | pending | approved | set_rank | denied | no_change
    prev_rank_key     TEXT,
    prev_submission_at TEXT,
    decided_rank      TEXT,
    decided_by        INTEGER,
    review_channel_id INTEGER,
    review_message_id INTEGER,
    reminded          INTEGER NOT NULL DEFAULT 0,
    image_path        TEXT,
    created_at        TEXT NOT NULL,
    decided_at        TEXT
);
CREATE INDEX idx_stats_user ON stats_submissions(discord_id, created_at);
CREATE INDEX idx_stats_status ON stats_submissions(status);

CREATE TABLE vouches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER NOT NULL,
    voucher_id   INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    counted      INTEGER NOT NULL,
    reason       TEXT,           -- why not counted (mod-side only)
    text         TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_vouch_pair ON vouches(voucher_id, recipient_id, created_at);

CREATE TABLE jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    poster_id         INTEGER NOT NULL,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL,
    tier              TEXT NOT NULL,
    status            TEXT NOT NULL,  -- pending_review | open | accepted | awaiting_confirm | completed | cancelled | expired | rejected
    helper_id         INTEGER,
    flag_reasons      TEXT NOT NULL DEFAULT '[]',
    channel_id        INTEGER,
    message_id        INTEGER,
    thread_id         INTEGER,
    review_channel_id INTEGER,
    review_message_id INTEGER,
    has_image         INTEGER NOT NULL DEFAULT 0,
    awarded_points    INTEGER,
    created_at        TEXT NOT NULL,
    posted_at         TEXT,
    accepted_at       TEXT,
    closed_at         TEXT
);
CREATE INDEX idx_jobs_poster ON jobs(poster_id, created_at);
CREATE INDEX idx_jobs_status ON jobs(status);

CREATE TABLE job_flags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    flagger_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
