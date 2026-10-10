-- One row per rewarded raider per job (a squad job rewards the confirmed raider and their squad);
-- other jobs reward exactly one. Caps (pair, daily) are counted from here.
CREATE TABLE job_rewards (
    job_id     INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    place      INTEGER NOT NULL,       -- 1 = fastest successful completion
    points     INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, user_id)
);
CREATE INDEX idx_job_rewards_user ON job_rewards(user_id, created_at);

-- reward_mode: single | placed | full (how the reward was split when the job was confirmed)
ALTER TABLE jobs ADD COLUMN reward_mode TEXT;

INSERT INTO job_rewards(job_id, user_id, place, points, created_at)
    SELECT id, helper_id, 1, COALESCE(awarded_points, 0), COALESCE(closed_at, created_at) FROM jobs
    WHERE status = 'completed' AND helper_id IS NOT NULL;
