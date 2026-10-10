-- Challenge jobs (Guild Master only) replace squad jobs. A challenge stays open until the Guild Master closes it.
-- Anyone eligible forms a squad and invites teammates (who must accept); after the raid every member uploads their
-- post-raid summary screenshot; the Guild Master approves or rejects the squad. Every approved squad is rewarded
-- equally (first approved squad gets a bonus), each member at most once per challenge (job_rewards PK).
ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'job';   -- job | challenge
UPDATE jobs SET kind = 'challenge' WHERE squad = 1;
ALTER TABLE jobs DROP COLUMN squad;
DROP TABLE job_squad_members;

CREATE TABLE challenge_squads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER NOT NULL,
    leader_id         INTEGER NOT NULL,
    status            TEXT NOT NULL,   -- forming | submitting | in_review | approved | rejected | disbanded
    thread_id         INTEGER,
    place             INTEGER,         -- order among approved squads; 1 = first clear
    review_channel_id INTEGER,
    review_message_id INTEGER,
    created_at        TEXT NOT NULL,
    submitted_at      TEXT,
    decided_at        TEXT,
    decided_by        INTEGER
);
CREATE INDEX idx_challenge_squads_job ON challenge_squads(job_id, status);

CREATE TABLE challenge_members (
    squad_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    status       TEXT NOT NULL,        -- invited | accepted | declined | cancelled
    invited_at   TEXT NOT NULL,
    responded_at TEXT,
    PRIMARY KEY (squad_id, user_id)
);

CREATE TABLE challenge_proofs (
    squad_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    image_path   TEXT,
    sha256       TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (squad_id, user_id)
);
