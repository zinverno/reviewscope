-- Dedicated database. Provision rs_runtime as an unprivileged cluster login before upgrade.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $$ BEGIN
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END $$;
ALTER TABLE workspace_membership ADD FOREIGN KEY (principal_id) REFERENCES principal;
CREATE INDEX membership_by_principal ON workspace_membership(principal_id,workspace_id) WHERE state='active';

CREATE TABLE access_session (
    token_hash bytea PRIMARY KEY CHECK (length(token_hash)=32),
    principal_id uuid NOT NULL REFERENCES principal,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '8 hours'),
    revoked_at timestamptz,
    CHECK (expires_at > created_at)
);
CREATE FUNCTION invalidate_sessions() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    UPDATE access_session SET revoked_at=clock_timestamp()
    WHERE principal_id=NEW.id AND revoked_at IS NULL;
    RETURN NULL;
END $$;
CREATE TRIGGER deactivate_sessions AFTER UPDATE OF state ON principal
FOR EACH ROW WHEN (OLD.state='active' AND NEW.state='inactive') EXECUTE FUNCTION invalidate_sessions();

CREATE TABLE membership_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    workspace_id uuid NOT NULL REFERENCES workspace,
    subject_id uuid NOT NULL REFERENCES principal,
    actor_id uuid REFERENCES principal,
    old_role text, old_state text,
    new_role text, new_state text
);

-- All names in definer bodies resolve in trusted schemas. No dynamic SQL in callable functions.
CREATE FUNCTION access_actor() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT s.principal_id FROM access_session s JOIN principal p ON p.id=s.principal_id
    WHERE s.token_hash=sha256(convert_to(nullif(current_setting('rs.session_proof',true),''),'UTF8'))
      AND s.revoked_at IS NULL AND s.expires_at > statement_timestamp() AND p.state='active'
$$;
CREATE FUNCTION login_salt(account text) RETURNS bytea LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT c.salt FROM principal p JOIN password_credential c ON c.principal_id=p.id
    WHERE p.login=account AND p.state='active'
$$;
CREATE FUNCTION open_session(account text, password_proof bytea, digest bytea) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    WITH issued AS (
        INSERT INTO access_session(token_hash,principal_id)
        SELECT digest,p.id FROM principal p JOIN password_credential c ON c.principal_id=p.id
        WHERE p.login=account AND p.state='active' AND c.verifier=sha256(password_proof)
        RETURNING 1
    ) SELECT EXISTS (SELECT FROM issued)
$$;
CREATE FUNCTION close_session() RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
    UPDATE access_session SET revoked_at=clock_timestamp()
    WHERE token_hash=sha256(convert_to(nullif(current_setting('rs.session_proof',true),''),'UTF8'))
      AND revoked_at IS NULL
$$;

CREATE FUNCTION record_membership() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    INSERT INTO membership_event(workspace_id,subject_id,actor_id,old_role,old_state,new_role,new_state)
    VALUES (coalesce(NEW.workspace_id,OLD.workspace_id),coalesce(NEW.principal_id,OLD.principal_id),
            access_actor(),OLD.role,OLD.state,NEW.role,NEW.state);
    RETURN NULL;
END $$;
CREATE TRIGGER membership_changes AFTER INSERT OR UPDATE OR DELETE ON workspace_membership
FOR EACH ROW EXECUTE FUNCTION record_membership();
INSERT INTO membership_event(workspace_id,subject_id,new_role,new_state)
SELECT workspace_id,principal_id,role,state FROM workspace_membership;

CREATE FUNCTION change_member(w uuid, subject uuid, next_role text, next_state text) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE actor_role text;
BEGIN
    IF w IS DISTINCT FROM nullif(current_setting('rs.workspace',true),'')::uuid THEN RETURN 404; END IF;
    -- Check scope before taking a lock, then recheck after concurrent owner changes.
    SELECT role INTO actor_role FROM workspace_membership
      WHERE workspace_id=w AND principal_id=access_actor() AND state='active';
    IF actor_role IS NULL THEN RETURN 404; END IF;
    IF actor_role <> 'owner' THEN RETURN 403; END IF;
    PERFORM FROM workspace WHERE id=w FOR UPDATE;
    SELECT role INTO actor_role FROM workspace_membership
      WHERE workspace_id=w AND principal_id=access_actor() AND state='active';
    IF actor_role IS NULL THEN RETURN 404; END IF;
    IF actor_role <> 'owner' THEN RETURN 403; END IF;
    IF subject=access_actor() THEN RETURN 403; END IF;
    UPDATE workspace_membership SET role=next_role,state=next_state
      WHERE workspace_id=w AND principal_id=subject;
    IF NOT FOUND THEN RETURN 404; END IF;
    RETURN 204;
END $$;

REVOKE EXECUTE ON FUNCTION access_actor(),login_salt(text),open_session(text,bytea,bytea),
    close_session(),change_member(uuid,uuid,text,text),record_membership(),invalidate_sessions() FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO rs_runtime;
GRANT EXECUTE ON FUNCTION access_actor(),login_salt(text),open_session(text,bytea,bytea),
    close_session(),change_member(uuid,uuid,text,text) TO rs_runtime;
GRANT SELECT(version_num) ON alembic_version TO rs_runtime;
GRANT SELECT(workspace_id,principal_id,role,state) ON workspace_membership TO rs_runtime;
GRANT SELECT(id,name,state) ON workspace TO rs_runtime;
GRANT SELECT(workspace_id,id,kind) ON source TO rs_runtime;
GRANT SELECT(workspace_id,id,source_id,created_at) ON snapshot TO rs_runtime;
GRANT SELECT(workspace_id,id,snapshot_id,status) ON analysis_run TO rs_runtime;

ALTER TABLE workspace_membership ENABLE ROW LEVEL SECURITY;
CREATE POLICY self_membership ON workspace_membership FOR SELECT TO rs_runtime
USING (state='active' AND principal_id=(SELECT access_actor()));
ALTER TABLE workspace ENABLE ROW LEVEL SECURITY;
CREATE POLICY visible_workspace ON workspace FOR SELECT TO rs_runtime
USING (EXISTS (SELECT FROM workspace_membership m WHERE m.workspace_id=workspace.id));

DO $$
DECLARE tab text;
BEGIN
    FOREACH tab IN ARRAY ARRAY['client_project','location','source','source_location','import_batch',
        'review_identity','review_version','snapshot','snapshot_member','analysis_run','artifact',
        'membership_event'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tab);
        IF tab IN ('source','snapshot','analysis_run') THEN
            EXECUTE format('CREATE POLICY scoped_read ON %I FOR SELECT TO rs_runtime USING (
                workspace_id=nullif(current_setting(''rs.workspace'',true),'''')::uuid AND
                EXISTS (SELECT FROM workspace_membership m WHERE m.workspace_id=%I.workspace_id))',tab,tab);
        END IF;
    END LOOP;
END $$;
