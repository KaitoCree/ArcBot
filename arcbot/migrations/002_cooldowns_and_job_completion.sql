-- Promotion: cooldown end stored explicitly so a "no change" result can use a shorter wait.
ALTER TABLE users ADD COLUMN stats_cooldown_until TEXT;
ALTER TABLE stats_submissions ADD COLUMN prev_cooldown_until TEXT;

-- Jobs: either side can start completion; check-in reminders; mod resolution of stalled confirmations.
ALTER TABLE jobs ADD COLUMN completion_requested_by INTEGER;
ALTER TABLE jobs ADD COLUMN completion_requested_at TEXT;
ALTER TABLE jobs ADD COLUMN reminders_sent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN notice_message_id INTEGER;

CREATE INDEX idx_vouch_recipient ON vouches(recipient_id, created_at);
