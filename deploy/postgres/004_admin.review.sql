-- Run as migration superuser after 001 and 002 in the dedicated database, after approval.
-- Adds two management tables alongside the unchanged 11 application tables.
-- Kept separate from SQLAlchemy automatic creation; management explicitly requires this migration.
BEGIN;
SET LOCAL lock_timeout = '5s';

-- Internal function owner, never assigned to a human, app, or device login.
CREATE ROLE rp1_monitor_admin_executor NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT USAGE ON SCHEMA rp1_monitor TO rp1_monitor_admin_executor;

CREATE TABLE rp1_monitor.monitor_metric_corrections (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  window_id bigint NOT NULL REFERENCES rp1_monitor.monitor_windows(id),
  metric_key text NOT NULL,
  previous_id bigint REFERENCES rp1_monitor.monitor_metric_corrections(id),
  operation text NOT NULL CHECK (operation IN ('replace','exclude','restore')),
  original_payload_hash text NOT NULL CHECK (original_payload_hash ~ '^[0-9a-f]{64}$'),
  original_metric json NOT NULL,
  replacement jsonb,
  reason text NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 1 AND 1000),
  actor_db_login text NOT NULL,
  created_at timestamptz NOT NULL,
  UNIQUE NULLS NOT DISTINCT (window_id, metric_key, previous_id),
  CHECK ((operation = 'replace' AND replacement IS NOT NULL)
      OR (operation <> 'replace' AND replacement IS NULL))
);
CREATE INDEX ix_monitor_corrections_target
  ON rp1_monitor.monitor_metric_corrections(window_id, metric_key, id DESC);
CREATE INDEX ix_monitor_corrections_previous ON rp1_monitor.monitor_metric_corrections(previous_id);

CREATE TABLE rp1_monitor.monitor_admin_audit (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  robot_id text NOT NULL REFERENCES rp1_monitor.monitor_robots(robot_id),
  correction_id bigint REFERENCES rp1_monitor.monitor_metric_corrections(id),
  action text NOT NULL CHECK (action IN ('robot.created','robot.updated',
    'metric.replace','metric.exclude','metric.restore')),
  before_value jsonb,
  after_value jsonb NOT NULL,
  reason text NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 1 AND 1000),
  actor_db_login text NOT NULL,
  created_at timestamptz NOT NULL
);
CREATE INDEX ix_monitor_admin_audit_robot_id ON rp1_monitor.monitor_admin_audit(robot_id, id);
CREATE INDEX ix_monitor_admin_audit_correction ON rp1_monitor.monitor_admin_audit(correction_id);

REVOKE ALL ON rp1_monitor.monitor_metric_corrections, rp1_monitor.monitor_admin_audit FROM PUBLIC;
REVOKE ALL ON SEQUENCE rp1_monitor.monitor_metric_corrections_id_seq,
  rp1_monitor.monitor_admin_audit_id_seq FROM PUBLIC;
GRANT SELECT ON rp1_monitor.monitor_metric_corrections, rp1_monitor.monitor_admin_audit,
  rp1_monitor.monitor_windows TO rp1_monitor_admin;

-- The function owner has no table ownership, DDL, raw-evidence UPDATE, or DELETE rights.
GRANT SELECT (robot_id, name, source_kind, enabled), INSERT,
  UPDATE (name, enabled) ON rp1_monitor.monitor_robots TO rp1_monitor_admin_executor;
GRANT SELECT ON rp1_monitor.monitor_windows TO rp1_monitor_admin_executor;
GRANT SELECT, INSERT ON rp1_monitor.monitor_metric_corrections TO rp1_monitor_admin_executor;
GRANT INSERT ON rp1_monitor.monitor_admin_audit TO rp1_monitor_admin_executor;
GRANT USAGE ON SEQUENCE rp1_monitor.monitor_metric_corrections_id_seq,
  rp1_monitor.monitor_admin_audit_id_seq TO rp1_monitor_admin_executor;

CREATE FUNCTION rp1_monitor.admin_create_robot(
  p_robot_id text, p_name text, p_source_kind text, p_token_digest text, p_reason text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
  IF p_robot_id IS NULL OR p_robot_id !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'
     OR p_name IS NULL OR char_length(btrim(p_name)) NOT BETWEEN 1 AND 128
     OR p_source_kind IS NULL OR p_source_kind NOT IN ('simulated','ros2')
     OR p_token_digest IS NULL OR p_token_digest !~ '^[0-9a-f]{64}$'
     OR p_reason IS NULL OR char_length(btrim(p_reason)) NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'Invalid registration or missing reason' USING ERRCODE = '22023';
  END IF;
  -- Digest only: the plaintext high-entropy device key is provisioned through a private channel.
  INSERT INTO rp1_monitor.monitor_robots(robot_id, name, source_kind, token_digest, enabled)
    VALUES (p_robot_id, btrim(p_name), p_source_kind, p_token_digest, true);
  INSERT INTO rp1_monitor.monitor_admin_audit(
    robot_id, action, before_value, after_value, reason, actor_db_login, created_at
  ) VALUES (p_robot_id, 'robot.created', NULL,
    jsonb_build_object('name', btrim(p_name), 'source_kind', p_source_kind, 'enabled', true),
    btrim(p_reason), session_user, clock_timestamp());
  RETURN p_robot_id;
END;
$function$;

CREATE FUNCTION rp1_monitor.admin_update_robot(
  p_robot_id text, p_expected_name text, p_expected_enabled boolean,
  p_name text, p_enabled boolean, p_reason text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
  v_old record;
BEGIN
  IF p_name IS NULL OR char_length(btrim(p_name)) NOT BETWEEN 1 AND 128
     OR p_enabled IS NULL OR p_reason IS NULL
     OR char_length(btrim(p_reason)) NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'Invalid update or missing reason' USING ERRCODE = '22023';
  END IF;
  SELECT r.robot_id, r.name, r.source_kind, r.enabled INTO v_old
    FROM rp1_monitor.monitor_robots r WHERE r.robot_id = p_robot_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Robot not found' USING ERRCODE = '22023';
  END IF;
  IF v_old.name IS DISTINCT FROM p_expected_name
     OR v_old.enabled IS DISTINCT FROM p_expected_enabled THEN
    RAISE EXCEPTION 'Robot changed; reload before editing' USING ERRCODE = '40001';
  END IF;
  UPDATE rp1_monitor.monitor_robots SET name = btrim(p_name), enabled = p_enabled
    WHERE robot_id = p_robot_id;
  INSERT INTO rp1_monitor.monitor_admin_audit(
    robot_id, action, before_value, after_value, reason, actor_db_login, created_at
  ) VALUES (p_robot_id, 'robot.updated',
    jsonb_build_object('name', v_old.name, 'source_kind', v_old.source_kind, 'enabled', v_old.enabled),
    jsonb_build_object('name', btrim(p_name), 'source_kind', v_old.source_kind, 'enabled', p_enabled),
    btrim(p_reason), session_user, clock_timestamp());
END;
$function$;

CREATE FUNCTION rp1_monitor.admin_correct_metric(
  p_window_id bigint, p_metric_key text, p_expected_revision bigint,
  p_operation text, p_replacement jsonb, p_reason text
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
  v_window record;
  v_original json;
  v_original_doc jsonb;
  v_previous_id bigint;
  v_id bigint;
  v_before jsonb;
BEGIN
  IF p_window_id IS NULL OR p_window_id <= 0 OR p_metric_key IS NULL
     OR p_operation IS NULL OR p_operation NOT IN ('replace','exclude','restore')
     OR p_reason IS NULL OR char_length(btrim(p_reason)) NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'Invalid correction or missing reason' USING ERRCODE = '22023';
  END IF;
  -- Same immutable window always serializes; no UPDATE privilege on original evidence is needed.
  PERFORM pg_advisory_xact_lock(p_window_id);
  SELECT w.robot_id, w.payload_hash, w.payload INTO v_window
    FROM rp1_monitor.monitor_windows w WHERE w.id = p_window_id;
  IF NOT FOUND OR (v_window.payload -> 'metrics' -> p_metric_key) IS NULL THEN
    RAISE EXCEPTION 'Original metric not found' USING ERRCODE = '22023';
  END IF;
  v_original := v_window.payload -> 'metrics' -> p_metric_key;
  v_original_doc := v_original::jsonb;
  SELECT c.id, to_jsonb(c) - 'actor_db_login' INTO v_previous_id, v_before
    FROM rp1_monitor.monitor_metric_corrections c
    WHERE c.window_id = p_window_id AND c.metric_key = p_metric_key
    ORDER BY c.id DESC LIMIT 1;
  IF v_previous_id IS DISTINCT FROM p_expected_revision THEN
    RAISE EXCEPTION 'Correction changed; reload before editing' USING ERRCODE = '40001';
  END IF;
  IF p_operation = 'replace' THEN
    -- Correct observed values only. Never invent missing samples, alter count/age, add fields or units.
    IF p_replacement IS NULL OR jsonb_typeof(p_replacement) <> 'object'
       OR NOT (p_replacement ?& ARRAY['last','minimum','maximum','count','age_ms'])
       OR (p_replacement - ARRAY['last','minimum','maximum'])
          IS DISTINCT FROM (v_original_doc - ARRAY['last','minimum','maximum'])
       OR coalesce((v_original_doc ->> 'count')::bigint, 0) <= 0
       OR jsonb_typeof(p_replacement -> 'last') IS DISTINCT FROM 'number' THEN
      RAISE EXCEPTION 'Replacement must preserve sample count, age and original fields'
        USING ERRCODE = '22023';
    END IF;
    PERFORM (p_replacement ->> 'last')::double precision;
    IF v_original_doc -> 'minimum' = 'null'::jsonb THEN
      IF p_replacement -> 'minimum' IS DISTINCT FROM 'null'::jsonb
         OR p_replacement -> 'maximum' IS DISTINCT FROM 'null'::jsonb THEN
        RAISE EXCEPTION 'Discrete metric cannot acquire extrema' USING ERRCODE = '22023';
      END IF;
    ELSE
      IF jsonb_typeof(p_replacement -> 'minimum') IS DISTINCT FROM 'number'
         OR jsonb_typeof(p_replacement -> 'maximum') IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION 'Range metric requires both extrema' USING ERRCODE = '22023';
      END IF;
      PERFORM (p_replacement ->> 'minimum')::double precision,
        (p_replacement ->> 'maximum')::double precision;
      IF (p_replacement ->> 'minimum')::numeric > (p_replacement ->> 'last')::numeric
         OR (p_replacement ->> 'last')::numeric > (p_replacement ->> 'maximum')::numeric THEN
        RAISE EXCEPTION 'Last value must be within extrema' USING ERRCODE = '22023';
      END IF;
    END IF;
  ELSIF p_replacement IS NOT NULL THEN
    RAISE EXCEPTION 'Exclude/restore must not carry a replacement' USING ERRCODE = '22023';
  END IF;
  INSERT INTO rp1_monitor.monitor_metric_corrections(
    window_id, metric_key, previous_id, operation, original_payload_hash, original_metric,
    replacement, reason, actor_db_login, created_at
  ) VALUES (p_window_id, p_metric_key, v_previous_id, p_operation, v_window.payload_hash,
    v_original, p_replacement, btrim(p_reason), session_user, clock_timestamp()) RETURNING id INTO v_id;
  INSERT INTO rp1_monitor.monitor_admin_audit(
    robot_id, correction_id, action, before_value, after_value, reason, actor_db_login, created_at
  ) VALUES (v_window.robot_id, v_id, 'metric.' || p_operation, v_before,
    jsonb_build_object('window_id', p_window_id, 'metric_key', p_metric_key,
      'operation', p_operation, 'replacement', p_replacement, 'revision', v_id,
      'original_payload_hash', v_window.payload_hash),
    btrim(p_reason), session_user, clock_timestamp());
  RETURN v_id;
END;
$function$;

-- Ownership transfer needs CREATE temporarily; revoke it before the transaction commits.
GRANT CREATE ON SCHEMA rp1_monitor TO rp1_monitor_admin_executor;
ALTER FUNCTION rp1_monitor.admin_create_robot(text,text,text,text,text)
  OWNER TO rp1_monitor_admin_executor;
ALTER FUNCTION rp1_monitor.admin_update_robot(text,text,boolean,text,boolean,text)
  OWNER TO rp1_monitor_admin_executor;
ALTER FUNCTION rp1_monitor.admin_correct_metric(bigint,text,bigint,text,jsonb,text)
  OWNER TO rp1_monitor_admin_executor;
REVOKE CREATE ON SCHEMA rp1_monitor FROM rp1_monitor_admin_executor;
REVOKE ALL ON FUNCTION rp1_monitor.admin_create_robot(text,text,text,text,text),
  rp1_monitor.admin_update_robot(text,text,boolean,text,boolean,text),
  rp1_monitor.admin_correct_metric(bigint,text,bigint,text,jsonb,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rp1_monitor.admin_create_robot(text,text,text,text,text),
  rp1_monitor.admin_update_robot(text,text,boolean,text,boolean,text),
  rp1_monitor.admin_correct_metric(bigint,text,bigint,text,jsonb,text) TO rp1_monitor_admin;

-- Opt-in current correction projection. Raw telemetry/archives/alerts are unaffected.
CREATE VIEW rp1_monitor.admin_current_metric_corrections WITH (security_barrier=true) AS
  SELECT DISTINCT ON (c.window_id, c.metric_key)
    c.window_id, c.metric_key, w.robot_id, r.source_kind, w.stream_id, w.sequence,
    w.window_start, w.window_end, c.id AS revision, c.operation,
    CASE c.operation WHEN 'replace' THEN c.replacement WHEN 'restore'
      THEN c.original_metric::jsonb ELSE NULL END AS effective_metric,
    c.original_payload_hash, c.reason, c.actor_db_login, c.created_at
  FROM rp1_monitor.monitor_metric_corrections c
  JOIN rp1_monitor.monitor_windows w ON w.id = c.window_id
  JOIN rp1_monitor.monitor_robots r ON r.robot_id = w.robot_id
  ORDER BY c.window_id, c.metric_key, c.id DESC;
REVOKE ALL ON rp1_monitor.admin_current_metric_corrections FROM PUBLIC;
GRANT SELECT ON rp1_monitor.admin_current_metric_corrections TO rp1_monitor_admin;
COMMIT;
