-- Jobs: several raiders can attempt one job at once.
CREATE TABLE job_attempts (
    job_id    INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (job_id, user_id)
);

-- Every attempter can mark a job complete; the poster sees them in the order they did and confirms one.
-- rejected_at is set when the poster says that raider's completion wasn't real.
CREATE TABLE job_completions (
    job_id      INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    claimed_at  TEXT NOT NULL,
    rejected_at TEXT,
    PRIMARY KEY (job_id, user_id)
);

-- The private thread stays up after completion until the poster vouches for the helper (or a timeout).
ALTER TABLE jobs ADD COLUMN vouched_at TEXT;
ALTER TABLE jobs ADD COLUMN thread_closed_at TEXT;

-- Guild Master only: boost the hidden reward of a special post.
ALTER TABLE jobs ADD COLUMN xp_multiplier REAL NOT NULL DEFAULT 1;

-- Carry over single-helper jobs: the helper becomes the first attempter.
INSERT INTO job_attempts(job_id, user_id, joined_at)
    SELECT id, helper_id, COALESCE(accepted_at, created_at) FROM jobs
    WHERE helper_id IS NOT NULL AND status IN ('accepted', 'awaiting_confirm', 'needs_mod');

-- A pending single-helper completion becomes the first entry in the completion order.
INSERT INTO job_completions(job_id, user_id, claimed_at)
    SELECT id, helper_id, COALESCE(completion_requested_at, accepted_at, created_at) FROM jobs
    WHERE helper_id IS NOT NULL AND status IN ('awaiting_confirm', 'needs_mod');

-- In 'accepted' nobody has claimed completion yet; helper_id is now set only by "Mark complete".
UPDATE jobs SET helper_id = NULL WHERE status = 'accepted';

-- Threads of jobs that already finished are left alone.
UPDATE jobs SET thread_closed_at = COALESCE(closed_at, created_at) WHERE status NOT IN ('open', 'accepted', 'awaiting_confirm', 'needs_mod');
