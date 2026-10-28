-- REVIEW ONLY. Run as schema owner after 001, in the dedicated database only.
-- NOLOGIN group roles contain no passwords. Login users are provisioned after approval.
BEGIN;
CREATE ROLE rp1_monitor_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE rp1_monitor_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE rp1_monitor_archiver NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
-- Human management role; controlled write functions are defined in 004, not in the dashboard.
CREATE ROLE rp1_monitor_admin NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
REVOKE ALL ON SCHEMA rp1_monitor FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA rp1_monitor FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA rp1_monitor FROM PUBLIC;
GRANT USAGE ON SCHEMA rp1_monitor TO rp1_monitor_app, rp1_monitor_reader,
  rp1_monitor_archiver, rp1_monitor_admin;
GRANT rp1_monitor_reader TO rp1_monitor_admin;

-- App may register devices, update live snapshots and maintain alert heads/counters.
GRANT SELECT, INSERT, UPDATE ON rp1_monitor.monitor_robots,
  rp1_monitor.monitor_latest, rp1_monitor.monitor_daily_usage,
  rp1_monitor.monitor_relay_status, rp1_monitor.monitor_alert_heads TO rp1_monitor_app;
-- Evidence/history/config versions are append-only for the app.
GRANT SELECT, INSERT ON rp1_monitor.monitor_windows, rp1_monitor.monitor_key_windows,
  rp1_monitor.monitor_policy_versions, rp1_monitor.monitor_alert_events,
  rp1_monitor.monitor_diagnostic_records TO rp1_monitor_app;
GRANT SELECT ON rp1_monitor.monitor_archive_manifests TO rp1_monitor_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA rp1_monitor TO rp1_monitor_app;

-- Direct SQL readers do not receive device token digests or arbitrary raw reports.
CREATE VIEW rp1_monitor.dashboard_robots WITH (security_barrier=true) AS
  SELECT robot_id, name, source_kind, enabled FROM rp1_monitor.monitor_robots;
CREATE VIEW rp1_monitor.dashboard_key_windows WITH (security_barrier=true) AS
  SELECT k.robot_id, r.source_kind, k.window_id, w.stream_id, w.sequence,
    w.window_start, k.window_end, w.payload_hash, k.policy_sha256, k.metrics
  FROM rp1_monitor.monitor_key_windows k
  JOIN rp1_monitor.monitor_windows w ON w.id=k.window_id
  JOIN rp1_monitor.monitor_robots r ON r.robot_id=k.robot_id;
GRANT SELECT ON rp1_monitor.dashboard_robots, rp1_monitor.dashboard_key_windows,
  rp1_monitor.monitor_daily_usage, rp1_monitor.monitor_alert_heads,
  rp1_monitor.monitor_alert_events TO rp1_monitor_reader;

GRANT SELECT ON rp1_monitor.monitor_windows, rp1_monitor.monitor_key_windows,
  rp1_monitor.monitor_policy_versions TO rp1_monitor_archiver;
GRANT SELECT, INSERT ON rp1_monitor.monitor_archive_manifests TO rp1_monitor_archiver;

-- Login roles must explicitly set search_path=rp1_monitor,pg_catalog and timeouts.
-- Group role settings are not inherited by login members. No role receives DELETE/TRUNCATE/DDL.
-- Never grant app/archiver/executor membership to an administrator login.
-- Admin writes are enabled only by the separately reviewed 004_admin.review.sql.
COMMIT;
