-- REVIEW REQUIRED. Add monitoring recording history; never rewrites formal tests/MTBF.
-- Apply using the migration owner after 001/002 (004/005 may remain as installed).
-- This file has not been applied to the existing robot database by this change.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
CREATE TABLE rp1_monitor.monitor_recordings (
  id text PRIMARY KEY,
  run_number text NOT NULL UNIQUE,
  robot_id text NOT NULL REFERENCES rp1_monitor.monitor_robots(robot_id),
  source_kind text NOT NULL CONSTRAINT ck_recording_source CHECK (source_kind IN ('simulated','ros2')),
  case_number text NOT NULL CHECK (char_length(case_number) BETWEEN 1 AND 64),
  robot_number text NOT NULL CHECK (char_length(robot_number) BETWEEN 1 AND 64),
  site text NOT NULL CHECK (char_length(site) BETWEEN 1 AND 40),
  frequency_hz double precision NOT NULL CONSTRAINT ck_recording_hz CHECK (frequency_hz BETWEEN 0.001 AND 10),
  metrics jsonb NOT NULL CHECK (jsonb_typeof(metrics) = 'array' AND jsonb_array_length(metrics) BETWEEN 1 AND 256),
  source_context jsonb NOT NULL CHECK (jsonb_typeof(source_context) = 'object'),
  started_at timestamptz NOT NULL,
  ended_at timestamptz
);
CREATE INDEX ix_recording_robot_time ON rp1_monitor.monitor_recordings(robot_id, started_at);
CREATE TABLE rp1_monitor.monitor_recording_samples (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  recording_id text NOT NULL REFERENCES rp1_monitor.monitor_recordings(id),
  sequence bigint NOT NULL CONSTRAINT ck_recording_sequence CHECK (sequence > 0),
  observed_at timestamptz NOT NULL,
  payload json NOT NULL CHECK (json_typeof(payload) = 'object'),
  payload_hash text NOT NULL CONSTRAINT ck_recording_hash CHECK (char_length(payload_hash) = 64),
  CONSTRAINT uq_recording_sample UNIQUE (recording_id, sequence)
);
CREATE INDEX ix_recording_sample_time ON rp1_monitor.monitor_recording_samples(recording_id, observed_at);
REVOKE ALL ON rp1_monitor.monitor_recordings, rp1_monitor.monitor_recording_samples FROM PUBLIC;
REVOKE ALL ON SEQUENCE rp1_monitor.monitor_recording_samples_id_seq FROM PUBLIC;
GRANT SELECT, INSERT ON rp1_monitor.monitor_recordings, rp1_monitor.monitor_recording_samples TO rp1_monitor_app;
GRANT UPDATE(ended_at) ON rp1_monitor.monitor_recordings TO rp1_monitor_app;
GRANT USAGE, SELECT ON SEQUENCE rp1_monitor.monitor_recording_samples_id_seq TO rp1_monitor_app;
GRANT SELECT ON rp1_monitor.monitor_recordings, rp1_monitor.monitor_recording_samples
  TO rp1_monitor_reader, rp1_monitor_archiver;
-- No DELETE, TRUNCATE, UPDATE(sample evidence), CREATE, or schema ownership.
-- No partitioning or automatic retention deletion is introduced.
COMMIT;
