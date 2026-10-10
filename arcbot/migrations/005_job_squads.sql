-- Squad jobs (Guild Master only): the raider attempting adds squad members; when the poster confirms that raider,
-- the whole squad gets the full reward automatically.
ALTER TABLE jobs ADD COLUMN squad INTEGER NOT NULL DEFAULT 0;

CREATE TABLE job_squad_members (
    job_id    INTEGER NOT NULL,
    leader_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (job_id, member_id)
);
CREATE INDEX idx_job_squad_leader ON job_squad_members(job_id, leader_id);
