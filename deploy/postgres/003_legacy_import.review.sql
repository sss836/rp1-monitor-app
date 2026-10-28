-- OPTIONAL REVIEW TEMPLATE, NOT EXECUTED.
-- Preconditions: dedicated target database, empty rp1_monitor schema from 001;
-- reviewed legacy v0.4 tables already restored into legacy_monitor in THIS database.
-- Never run this directly against a production public schema. No source data is removed.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM rp1_monitor.monitor_robots) THEN
    RAISE EXCEPTION 'Target must be empty; compare conflicts instead of overwriting';
  END IF;
END $$;

INSERT INTO rp1_monitor.monitor_robots
  (robot_id, name, source_kind, token_digest, enabled, joint_names)
SELECT robot_id, name, source_kind, token_digest, enabled, joint_names::jsonb
FROM legacy_monitor.monitor_robots;

INSERT INTO rp1_monitor.monitor_windows
  (id, robot_id, stream_id, sequence, window_start, window_end, received_at, payload_hash, payload)
SELECT id, robot_id, stream_id, sequence, window_start, window_end, received_at, payload_hash, payload::json
FROM legacy_monitor.monitor_windows;

INSERT INTO rp1_monitor.monitor_latest
  (robot_id, stream_id, sequence, stream_first_seen, received_at, payload)
SELECT robot_id, stream_id, sequence, stream_first_seen, received_at, payload::jsonb
FROM legacy_monitor.monitor_latest;

INSERT INTO rp1_monitor.monitor_diagnostic_records
  (report_id, robot_id, created_at, sha256, payload)
SELECT report_id, robot_id, created_at, sha256, payload::json
FROM legacy_monitor.monitor_diagnostic_records;

DO $$ BEGIN
  IF (SELECT count(*) FROM legacy_monitor.monitor_windows) <> (SELECT count(*) FROM rp1_monitor.monitor_windows)
     OR (SELECT count(*) FROM legacy_monitor.monitor_diagnostic_records) <> (SELECT count(*) FROM rp1_monitor.monitor_diagnostic_records)
     OR EXISTS (
       SELECT 1 FROM legacy_monitor.monitor_windows s JOIN rp1_monitor.monitor_windows t USING(id)
       WHERE s.payload_hash <> t.payload_hash OR s.payload::text <> t.payload::text
         OR s.robot_id <> t.robot_id OR s.stream_id <> t.stream_id OR s.sequence <> t.sequence
         OR s.window_start <> t.window_start OR s.window_end <> t.window_end OR s.received_at <> t.received_at)
  THEN RAISE EXCEPTION 'Legacy evidence import mismatch; rollback required'; END IF;
END $$;
SELECT setval(pg_get_serial_sequence('rp1_monitor.monitor_windows','id'),
  coalesce((SELECT max(id) FROM rp1_monitor.monitor_windows), 1),
  EXISTS (SELECT 1 FROM rp1_monitor.monitor_windows));
COMMIT;
-- Next, validate original canonical hashes in the app, then explicitly backfill key windows.
-- This template preserves original source identity/timestamps and does not fabricate test IDs.
