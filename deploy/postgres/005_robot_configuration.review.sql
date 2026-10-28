-- Explicit additive migration after 001, 002 and 004. No telemetry is rewritten or removed.
BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE TABLE rp1_monitor.monitor_robot_config_versions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  robot_id text NOT NULL REFERENCES rp1_monitor.monitor_robots(robot_id),
  previous_id bigint REFERENCES rp1_monitor.monitor_robot_config_versions(id),
  configuration jsonb NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
  reason text NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 1 AND 1000),
  actor_db_login text NOT NULL,
  created_at timestamptz NOT NULL,
  UNIQUE NULLS NOT DISTINCT (robot_id, previous_id)
);
CREATE INDEX ix_monitor_config_robot_revision
  ON rp1_monitor.monitor_robot_config_versions(robot_id, id DESC);
CREATE INDEX ix_monitor_config_previous
  ON rp1_monitor.monitor_robot_config_versions(previous_id);
REVOKE ALL ON rp1_monitor.monitor_robot_config_versions FROM PUBLIC;
REVOKE ALL ON SEQUENCE rp1_monitor.monitor_robot_config_versions_id_seq FROM PUBLIC;
GRANT SELECT ON rp1_monitor.monitor_robot_config_versions TO rp1_monitor_app,
  rp1_monitor_admin, rp1_monitor_reader, rp1_monitor_archiver;
GRANT SELECT, INSERT ON rp1_monitor.monitor_robot_config_versions TO rp1_monitor_admin_executor;
GRANT USAGE ON SEQUENCE rp1_monitor.monitor_robot_config_versions_id_seq
  TO rp1_monitor_admin_executor;

ALTER TABLE rp1_monitor.monitor_admin_audit DROP CONSTRAINT monitor_admin_audit_action_check;
ALTER TABLE rp1_monitor.monitor_admin_audit ADD CONSTRAINT monitor_admin_audit_action_check
  CHECK (action IN ('robot.created','robot.updated','metric.replace','metric.exclude',
    'metric.restore','configuration.updated'));

CREATE FUNCTION rp1_monitor.admin_save_robot_configuration(
  p_robot_id text, p_expected_revision bigint, p_configuration jsonb, p_reason text
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
  v_source text;
  v_dof integer;
  v_previous bigint;
  v_before jsonb;
  v_id bigint;
  v_joint jsonb;
  v_limit record;
  v_index integer;
  -- Match Python str.strip(), so directly submitted SQL cannot store values the API rejects.
  v_whitespace constant text := E' \t\n\r\f' || U&'\000B\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000';
BEGIN
  IF p_reason IS NULL OR char_length(p_reason) NOT BETWEEN 1 AND 1000
     OR char_length(btrim(p_reason, v_whitespace)) = 0
     OR p_configuration IS NULL OR jsonb_typeof(p_configuration) <> 'object'
     OR NOT (p_configuration ?& ARRAY['schema_version','hardware_revision','dof',
       'physical_mapping_verified','scale_min_c','scale_max_c','joints','temperature_limits_c'])
     OR p_configuration - ARRAY['schema_version','hardware_revision','dof',
       'physical_mapping_verified','scale_min_c','scale_max_c','joints','temperature_limits_c'] <> '{}'::jsonb
     OR p_configuration -> 'schema_version' <> '1'::jsonb
     OR p_configuration -> 'dof' NOT IN ('24'::jsonb, '28'::jsonb)
     OR jsonb_typeof(p_configuration -> 'hardware_revision') <> 'string'
     OR char_length(p_configuration ->> 'hardware_revision') NOT BETWEEN 1 AND 120
     OR p_configuration ->> 'hardware_revision' <> btrim(p_configuration ->> 'hardware_revision', v_whitespace)
     OR jsonb_typeof(p_configuration -> 'physical_mapping_verified') <> 'boolean'
     OR jsonb_typeof(p_configuration -> 'scale_min_c') <> 'number'
     OR jsonb_typeof(p_configuration -> 'scale_max_c') <> 'number'
     OR jsonb_typeof(p_configuration -> 'joints') <> 'array'
     OR jsonb_typeof(p_configuration -> 'temperature_limits_c') <> 'object' THEN
    RAISE EXCEPTION 'Invalid monitoring configuration' USING ERRCODE = '22023';
  END IF;
  v_dof := (p_configuration ->> 'dof')::integer;
  PERFORM (p_configuration ->> 'scale_min_c')::double precision,
    (p_configuration ->> 'scale_max_c')::double precision;
  IF (p_configuration ->> 'scale_max_c')::numeric <= (p_configuration ->> 'scale_min_c')::numeric
     OR jsonb_array_length(p_configuration -> 'joints') <> v_dof THEN
    RAISE EXCEPTION 'Invalid display range or incomplete joint roster' USING ERRCODE = '22023';
  END IF;
  -- Same row lock as registration edits and ingestion; source never comes from the caller.
  SELECT r.source_kind INTO v_source FROM rp1_monitor.monitor_robots r
    WHERE r.robot_id = p_robot_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Robot not found' USING ERRCODE = '22023';
  END IF;
  FOR v_joint IN SELECT value FROM jsonb_array_elements(p_configuration -> 'joints') LOOP
    IF jsonb_typeof(v_joint) <> 'object'
       OR NOT (v_joint ?& ARRAY['index','joint_name','label','region','bus','motor_id'])
       OR v_joint - ARRAY['index','joint_name','label','region','bus','motor_id'] <> '{}'::jsonb
       OR jsonb_typeof(v_joint -> 'index') <> 'number'
       OR (v_joint ->> 'index') !~ '^(0|[1-9][0-9]*)$'
       OR jsonb_typeof(v_joint -> 'label') <> 'string'
       OR char_length(v_joint ->> 'label') NOT BETWEEN 1 AND 80
       OR v_joint ->> 'label' <> btrim(v_joint ->> 'label', v_whitespace)
       OR jsonb_typeof(v_joint -> 'joint_name') NOT IN ('string','null')
       OR jsonb_typeof(v_joint -> 'bus') NOT IN ('string','null')
       OR jsonb_typeof(v_joint -> 'motor_id') NOT IN ('string','null')
       OR jsonb_typeof(v_joint -> 'region') NOT IN ('string','null') THEN
      RAISE EXCEPTION 'Invalid joint entry' USING ERRCODE = '22023';
    END IF;
    v_index := (v_joint ->> 'index')::integer;
    IF v_index < 0 OR v_index >= v_dof
       OR (v_joint ->> 'joint_name' IS NOT NULL AND
           (char_length(v_joint ->> 'joint_name') NOT BETWEEN 1 AND 120
            OR v_joint ->> 'joint_name' <> btrim(v_joint ->> 'joint_name', v_whitespace)))
       OR (v_joint ->> 'bus' IS NOT NULL AND
           (char_length(v_joint ->> 'bus') NOT BETWEEN 1 AND 64
            OR v_joint ->> 'bus' <> btrim(v_joint ->> 'bus', v_whitespace)))
       OR (v_joint ->> 'motor_id' IS NOT NULL AND
           (char_length(v_joint ->> 'motor_id') NOT BETWEEN 1 AND 64
            OR v_joint ->> 'motor_id' <> btrim(v_joint ->> 'motor_id', v_whitespace)))
       OR (v_joint ->> 'bus' IS NULL) <> (v_joint ->> 'motor_id' IS NULL)
       OR (v_joint ->> 'region' IS NOT NULL AND v_joint ->> 'region' NOT IN
           ('left_shoulder','left_elbow','left_wrist','right_shoulder','right_elbow','right_wrist',
            'left_hip','left_knee','left_ankle','right_hip','right_knee','right_ankle'))
       OR (v_source = 'ros2' AND p_configuration -> 'physical_mapping_verified' = 'true'::jsonb
           AND v_joint ->> 'joint_name' IS NULL) THEN
      RAISE EXCEPTION 'Invalid joint identity, address or physical mapping' USING ERRCODE = '22023';
    END IF;
  END LOOP;
  IF (SELECT count(DISTINCT value ->> 'index') FROM jsonb_array_elements(p_configuration -> 'joints')) <> v_dof
     OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_configuration -> 'joints')
       WHERE value ->> 'joint_name' IS NOT NULL GROUP BY btrim(value ->> 'joint_name') HAVING count(*) > 1)
     OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_configuration -> 'joints')
       WHERE value ->> 'bus' IS NOT NULL GROUP BY btrim(value ->> 'bus'), btrim(value ->> 'motor_id') HAVING count(*) > 1) THEN
    RAISE EXCEPTION 'Duplicate joint identity or bus address' USING ERRCODE = '22023';
  END IF;
  FOR v_limit IN SELECT key, value FROM jsonb_each(p_configuration -> 'temperature_limits_c') LOOP
    IF v_limit.key !~ '^joint\.(0|[1-9][0-9]*)\.(motor|mos)_temperature$'
       OR jsonb_typeof(v_limit.value) <> 'number' THEN
      RAISE EXCEPTION 'Unsupported temperature criterion' USING ERRCODE = '22023';
    END IF;
    v_index := split_part(v_limit.key, '.', 2)::integer;
    PERFORM (v_limit.value #>> '{}')::double precision;
    IF v_index >= v_dof THEN
      RAISE EXCEPTION 'Temperature criterion exceeds joint roster' USING ERRCODE = '22023';
    END IF;
  END LOOP;
  SELECT c.id, c.configuration INTO v_previous, v_before
    FROM rp1_monitor.monitor_robot_config_versions c WHERE c.robot_id = p_robot_id
    ORDER BY c.id DESC LIMIT 1;
  IF v_previous IS DISTINCT FROM p_expected_revision THEN
    RAISE EXCEPTION 'Configuration changed; reload before editing' USING ERRCODE = '40001';
  END IF;
  INSERT INTO rp1_monitor.monitor_robot_config_versions(
    robot_id, previous_id, configuration, reason, actor_db_login, created_at
  ) VALUES (p_robot_id, v_previous, p_configuration, btrim(p_reason), session_user, clock_timestamp())
    RETURNING id INTO v_id;
  INSERT INTO rp1_monitor.monitor_admin_audit(
    robot_id, action, before_value, after_value, reason, actor_db_login, created_at
  ) VALUES (p_robot_id, 'configuration.updated', v_before,
    jsonb_build_object('revision', v_id, 'previous_revision', v_previous, 'configuration', p_configuration),
    btrim(p_reason), session_user, clock_timestamp());
  RETURN v_id;
EXCEPTION WHEN numeric_value_out_of_range OR invalid_text_representation THEN
  RAISE EXCEPTION 'Invalid numeric configuration' USING ERRCODE = '22023';
END;
$function$;

CREATE FUNCTION rp1_monitor.admin_create_robot_with_configuration(
  p_robot_id text, p_name text, p_source_kind text, p_token_digest text, p_reason text,
  p_configuration jsonb
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_revision bigint;
BEGIN
  PERFORM rp1_monitor.admin_create_robot(p_robot_id,p_name,p_source_kind,p_token_digest,p_reason);
  v_revision := rp1_monitor.admin_save_robot_configuration(p_robot_id,NULL,p_configuration,p_reason);
  RETURN v_revision;
END;
$function$;

GRANT CREATE ON SCHEMA rp1_monitor TO rp1_monitor_admin_executor;
ALTER FUNCTION rp1_monitor.admin_save_robot_configuration(text,bigint,jsonb,text)
  OWNER TO rp1_monitor_admin_executor;
ALTER FUNCTION rp1_monitor.admin_create_robot_with_configuration(text,text,text,text,text,jsonb)
  OWNER TO rp1_monitor_admin_executor;
REVOKE CREATE ON SCHEMA rp1_monitor FROM rp1_monitor_admin_executor;
REVOKE ALL ON FUNCTION rp1_monitor.admin_save_robot_configuration(text,bigint,jsonb,text),
  rp1_monitor.admin_create_robot_with_configuration(text,text,text,text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rp1_monitor.admin_save_robot_configuration(text,bigint,jsonb,text),
  rp1_monitor.admin_create_robot_with_configuration(text,text,text,text,text,jsonb) TO rp1_monitor_admin;
COMMIT;
