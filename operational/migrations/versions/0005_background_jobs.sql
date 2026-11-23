-- Existing runs keep their original identities, versions, results and transition contract.
ALTER TABLE analysis_run
    ADD COLUMN job_kind text,
    ADD COLUMN request_key uuid,
    ADD COLUMN requested_by uuid REFERENCES principal,
    ADD COLUMN attempt integer NOT NULL DEFAULT 0 CHECK (attempt BETWEEN 0 AND 3),
    ADD COLUMN lease_token uuid,
    ADD COLUMN lease_until timestamptz,
    ADD COLUMN heartbeat_at timestamptz,
    ADD COLUMN ready_at timestamptz,
    ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false,
    ADD COLUMN stage text CHECK (stage IN ('queued','verify','summarize','publish')),
    ADD COLUMN processed integer NOT NULL DEFAULT 0 CHECK (processed BETWEEN 0 AND 10000),
    ADD COLUMN total integer CHECK (total BETWEEN processed AND 10000),
    ADD COLUMN reason text CHECK (reason IN ('CANCELLED','ACCESS_REVOKED','LEASE_EXPIRED',
        'STORAGE_UNAVAILABLE','WORKER_STOPPED','INTEGRITY_ERROR','UNSUPPORTED_FORMAT',
        'RESOURCE_LIMIT','UNSUPPORTED_VERSION','INTERNAL_ERROR')),
    DROP CONSTRAINT analysis_run_status_check,
    DROP CONSTRAINT analysis_run_check,
    ADD CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    ADD CHECK ((status IN ('succeeded','failed','cancelled')) = (finished_at IS NOT NULL)),
    ADD CHECK ((job_kind IS NULL OR job_kind='legacy_manual_v1') AND num_nonnulls(request_key,requested_by,stage)=0
        OR (job_kind IS NOT NULL AND job_kind<>'legacy_manual_v1' AND num_nonnulls(request_key,requested_by,stage)=3)),
    ADD CHECK (request_key IS NULL OR (
        ((status='running')=(lease_token IS NOT NULL)) AND
        ((status='running')=(lease_until IS NOT NULL)) AND
        ((status='queued')=(ready_at IS NOT NULL)) AND
        (status<>'queued' OR (attempt<3 AND NOT cancel_requested)) AND
        (status<>'running' OR (attempt>0 AND heartbeat_at IS NOT NULL AND lease_until>heartbeat_at)) AND
        ((status IN ('failed','cancelled'))=(reason IS NOT NULL)) AND
        (status<>'cancelled' OR (cancel_requested AND reason='CANCELLED')) AND
        (status<>'failed' OR (NOT cancel_requested AND reason<>'CANCELLED')) AND
        (status<>'succeeded' OR (NOT cancel_requested AND stage='publish' AND total IS NOT NULL AND total=processed))
    )),
    ADD UNIQUE (workspace_id,request_key);
CREATE INDEX worker_queue ON analysis_run(ready_at,created_at) WHERE request_key IS NOT NULL AND status='queued';
CREATE INDEX worker_running ON analysis_run(lease_until) WHERE request_key IS NOT NULL AND status='running';
CREATE INDEX worker_requests ON analysis_run(workspace_id,created_at) WHERE request_key IS NOT NULL;

CREATE OR REPLACE FUNCTION guard_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.job_kind IS NOT NULL AND NEW.status='queued' AND NEW.attempt=0 AND NEW.processed=0 AND NOT NEW.cancel_requested
          AND (NEW.request_key IS NULL OR NEW.stage='queued') THEN RETURN NEW; END IF;
    ELSIF OLD.request_key IS NULL THEN
        IF (to_jsonb(NEW) - ARRAY['status','finished_at','output_artifact_id'])
            = (to_jsonb(OLD) - ARRAY['status','finished_at','output_artifact_id'])
          AND ((OLD.status='queued' AND NEW.status IN ('running','failed'))
            OR (OLD.status='running' AND NEW.status IN ('succeeded','failed'))) THEN RETURN NEW; END IF;
    ELSIF (NEW.workspace_id,NEW.id,NEW.snapshot_id,NEW.created_at,NEW.engine_version,NEW.definition_version,
           NEW.model_version,NEW.requested_by,NEW.job_kind,NEW.request_key) IS NOT DISTINCT FROM
          (OLD.workspace_id,OLD.id,OLD.snapshot_id,OLD.created_at,OLD.engine_version,OLD.definition_version,
           OLD.model_version,OLD.requested_by,OLD.job_kind,OLD.request_key)
        AND (NOT OLD.cancel_requested OR NEW.cancel_requested)
        AND ((OLD.status='queued' AND NEW.status='running' AND NEW.attempt=OLD.attempt+1)
          OR (OLD.status='queued' AND NEW.status='cancelled' AND NEW.attempt=OLD.attempt)
          OR (OLD.status='running' AND NEW.attempt=OLD.attempt AND
             ((NEW.status='running' AND NEW.lease_token=OLD.lease_token AND NEW.processed>=OLD.processed)
               OR NEW.status IN ('queued','failed','cancelled','succeeded')))) THEN RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid analysis run transition' USING ERRCODE='23514';
END $$;

-- Serialize admission with the PR-08 membership mutation lock. Recheck after acquiring it.
CREATE FUNCTION job_writer(w uuid) RETURNS integer LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE actor uuid := access_actor(); role_name text;
BEGIN
    IF w IS DISTINCT FROM nullif(current_setting('rs.workspace',true),'')::uuid OR actor IS NULL
       OR NOT EXISTS (SELECT FROM workspace_membership WHERE workspace_id=w AND principal_id=actor AND state='active')
       THEN RETURN 404; END IF;
    PERFORM FROM workspace WHERE id=w AND state='active' FOR UPDATE;
    IF NOT FOUND THEN RETURN 404; END IF;
    SELECT role INTO role_name FROM workspace_membership WHERE workspace_id=w AND principal_id=access_actor()
        AND state='active' FOR SHARE;
    IF NOT FOUND THEN RETURN 404; END IF;
    IF role_name NOT IN ('owner','analyst') THEN RETURN 403; END IF;
    RETURN 200;
END $$;

CREATE FUNCTION enqueue_job(w uuid,s uuid,k uuid,kind text,version text)
RETURNS TABLE(code integer,run_id uuid) LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE prior analysis_run; permitted integer := job_writer(w);
BEGIN
    IF permitted<>200 THEN RETURN QUERY SELECT permitted,NULL::uuid; RETURN; END IF;
    IF NOT EXISTS (SELECT FROM snapshot WHERE workspace_id=w AND id=s)
        THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
    SELECT * INTO prior FROM analysis_run WHERE workspace_id=w AND request_key=k;
    IF FOUND THEN
        IF (prior.snapshot_id,prior.job_kind,prior.engine_version) IS DISTINCT FROM (s,kind,version)
            THEN RETURN QUERY SELECT 409,NULL::uuid;
        ELSE RETURN QUERY SELECT 200,prior.id; END IF;
        RETURN;
    END IF;
    IF k IS NULL OR kind IS DISTINCT FROM 'snapshot_integrity_and_summary_v1' OR version IS DISTINCT FROM '1'
        THEN RETURN QUERY SELECT 422,NULL::uuid; RETURN; END IF;
    IF (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL
        AND status IN ('queued','running'))>=4 OR
       (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND
        created_at>clock_timestamp()-interval '1 hour')>=20 THEN RETURN QUERY SELECT 429,NULL::uuid; RETURN; END IF;
    INSERT INTO analysis_run(workspace_id,snapshot_id,job_kind,request_key,requested_by,engine_version,
        definition_version,model_version,stage,ready_at) VALUES
        (w,s,kind,k,access_actor(),version,'summary-1','unused','queued',clock_timestamp())
        RETURNING id INTO run_id;
    code:=202; RETURN NEXT;
END $$;

CREATE FUNCTION cancel_job(w uuid,r uuid) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run; permitted integer := job_writer(w);
BEGIN
    IF permitted<>200 THEN RETURN permitted; END IF;
    SELECT * INTO j FROM analysis_run WHERE workspace_id=w AND id=r AND request_key IS NOT NULL FOR UPDATE;
    IF NOT FOUND THEN RETURN 404; END IF;
    IF j.status='queued' THEN
        UPDATE analysis_run SET status='cancelled',cancel_requested=true,reason='CANCELLED',
            finished_at=clock_timestamp(),ready_at=NULL WHERE workspace_id=w AND id=r;
    ELSIF j.status='running' THEN
        UPDATE analysis_run SET cancel_requested=true WHERE workspace_id=w AND id=r;
    ELSIF j.status<>'cancelled' THEN RETURN 409; END IF;
    RETURN 200;
END $$;

-- The role can read only the private inputs of a currently live attempt through these functions.
CREATE FUNCTION job_authorized(j analysis_run) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
    SELECT EXISTS (SELECT FROM workspace w JOIN workspace_membership m ON m.workspace_id=w.id
        JOIN principal p ON p.id=m.principal_id WHERE w.id=j.workspace_id AND w.state='active'
        AND p.id=j.requested_by AND p.state='active' AND m.state='active' AND m.role IN ('owner','analyst'))
$$;

CREATE FUNCTION stop_attempt(j analysis_run,why text) RETURNS text LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE again boolean := j.attempt<3 AND why IN ('LEASE_EXPIRED','WORKER_STOPPED','STORAGE_UNAVAILABLE');
DECLARE outcome text := CASE WHEN j.cancel_requested THEN 'cancelled' WHEN again THEN 'queued' ELSE 'failed' END;
BEGIN
    UPDATE analysis_run SET status=outcome,lease_until=NULL,lease_token=NULL,
        finished_at=CASE WHEN outcome<>'queued' THEN clock_timestamp() END,
        reason=CASE WHEN j.cancel_requested THEN 'CANCELLED' WHEN NOT again THEN why END,
        ready_at=CASE WHEN outcome='queued' THEN clock_timestamp()+j.attempt*interval '1 second' END,
        stage=CASE WHEN outcome='queued' THEN 'queued' ELSE stage END
        WHERE workspace_id=j.workspace_id AND id=j.id;
    RETURN outcome;
END $$;

CREATE FUNCTION claim_job() RETURNS TABLE(w uuid,r uuid,token uuid,generation integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run;
BEGIN
    -- Only admission to the two live worker slots is serialized, never file processing.
    PERFORM pg_advisory_xact_lock(11713,11);
    SELECT * INTO j FROM analysis_run WHERE request_key IS NOT NULL AND status='running'
        AND lease_until<=clock_timestamp() ORDER BY lease_until LIMIT 1 FOR UPDATE SKIP LOCKED;
    IF FOUND THEN PERFORM stop_attempt(j,'LEASE_EXPIRED'); END IF;
    IF (SELECT count(*) FROM analysis_run WHERE request_key IS NOT NULL AND status='running'
        AND lease_until>clock_timestamp())>=2 THEN RETURN; END IF;
    SELECT * INTO j FROM analysis_run WHERE request_key IS NOT NULL AND status='queued'
        AND ready_at<=clock_timestamp() ORDER BY ready_at,created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN RETURN; END IF;
    RETURN QUERY UPDATE analysis_run SET status='running',stage='verify',attempt=attempt+1,processed=0,total=NULL,
        lease_token=gen_random_uuid(),heartbeat_at=clock_timestamp(),lease_until=clock_timestamp()+interval '15 seconds',
        ready_at=NULL WHERE workspace_id=j.workspace_id AND id=j.id RETURNING workspace_id,id,lease_token,attempt;
END $$;

CREATE FUNCTION worker_step(w uuid,r uuid,t uuid,next_stage text,n integer,den integer,
    problem text DEFAULT NULL, digest sha256_hex DEFAULT NULL, bytes integer DEFAULT NULL, manifest sha256_hex DEFAULT NULL)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run; out_id uuid; allowed boolean;
BEGIN
    -- Same lock order as cancellation: workspace and membership, then run. Recheck the fence under lock.
    SELECT * INTO j FROM analysis_run WHERE workspace_id=w AND id=r AND lease_token=t;
    IF NOT FOUND THEN RETURN 'stale'; END IF;
    PERFORM FROM workspace WHERE id=w AND state='active' FOR SHARE;
    allowed := FOUND;
    PERFORM FROM workspace_membership m JOIN principal p ON p.id=m.principal_id WHERE m.workspace_id=w
        AND p.id=j.requested_by AND p.state='active' AND m.state='active' AND m.role IN ('owner','analyst')
        FOR SHARE OF m,p;
    allowed := allowed AND FOUND;
    SELECT * INTO j FROM analysis_run WHERE workspace_id=w AND id=r FOR UPDATE;
    IF NOT FOUND OR j.status<>'running' OR j.lease_token IS DISTINCT FROM t OR j.lease_until<=clock_timestamp()
        THEN RETURN 'stale'; END IF;
    IF j.cancel_requested THEN RETURN stop_attempt(j,'CANCELLED'); END IF;
    IF NOT allowed THEN RETURN stop_attempt(j,'ACCESS_REVOKED'); END IF;
    IF problem IS NOT NULL THEN
        IF problem NOT IN ('STORAGE_UNAVAILABLE','WORKER_STOPPED','INTEGRITY_ERROR','UNSUPPORTED_FORMAT',
            'RESOURCE_LIMIT','UNSUPPORTED_VERSION','INTERNAL_ERROR') THEN RAISE EXCEPTION 'invalid reason'; END IF;
        RETURN stop_attempt(j,problem);
    END IF;
    IF digest IS NOT NULL THEN
        IF j.stage<>'publish' OR j.total IS NULL OR j.processed<>j.total OR bytes IS NULL OR bytes NOT BETWEEN 1 AND 65536
           OR NOT EXISTS (SELECT FROM snapshot WHERE workspace_id=w AND id=j.snapshot_id AND manifest_sha256=manifest)
           THEN RAISE EXCEPTION 'invalid publication'; END IF;
        IF j.lease_until<=clock_timestamp() THEN RETURN 'stale'; END IF;
        INSERT INTO artifact(workspace_id,run_id,kind,checksum,size_bytes)
            VALUES(w,r,'json',digest,bytes) RETURNING id INTO out_id;
        UPDATE analysis_run SET status='succeeded',finished_at=clock_timestamp(),output_artifact_id=out_id,
            lease_token=NULL,lease_until=NULL WHERE workspace_id=w AND id=r;
        RETURN 'succeeded';
    END IF;
    IF next_stage IS NULL OR next_stage NOT IN ('verify','summarize','publish') OR n IS NULL
      OR (j.total IS NOT NULL AND den IS DISTINCT FROM j.total)
      OR (next_stage='publish' AND (den IS NULL OR n<>den))
      OR (j.stage='publish' AND next_stage<>'publish') OR (j.stage='summarize' AND next_stage='verify')
      THEN RAISE EXCEPTION 'invalid progress'; END IF;
    UPDATE analysis_run SET stage=next_stage,processed=n,total=den,heartbeat_at=clock_timestamp(),
        lease_until=clock_timestamp()+interval '15 seconds' WHERE workspace_id=w AND id=r;
    RETURN 'running';
END $$;

CREATE FUNCTION live_job(w uuid,r uuid,t uuid) RETURNS SETOF analysis_run LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
    SELECT j.* FROM analysis_run j WHERE j.workspace_id=w AND j.id=r AND j.lease_token=t AND j.status='running'
        AND j.lease_until>statement_timestamp() AND NOT j.cancel_requested AND job_authorized(j)
$$;

CREATE FUNCTION job_input(w uuid,r uuid,t uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
    SELECT jsonb_build_object('root',ws.delivery_object_root,'snapshot_id',s.id,'manifest',s.manifest_sha256,
        'source',s.source_id,'batch',s.batch_id,'filters',s.filters,'snapshot_sha256',s.duckdb_sha256,
        'valid',s.manifest_sha256=snapshot_digest(w,s.source_id,s.batch_id,s.duckdb_sha256,s.filters,s.version_ids)
            AND s.version_ids=ARRAY(SELECT version_id FROM snapshot_member WHERE workspace_id=w AND snapshot_id=s.id ORDER BY 1),
        'ids',CASE WHEN cardinality(s.version_ids)<=10000 THEN s.version_ids END,
        'input',b.input_count,'accepted',b.accepted_count,'coverage',b.coverage,'delivery',b.delivery,'file_sha256',b.file_sha256,
        'rejected',b.rejected_count,'duplicates',b.confirmed_duplicate_count,'procedure',b.procedure_version,
        'kind',j.job_kind,'version',j.engine_version,'model',j.model_version,'definition',j.definition_version,
        'reasons',(SELECT jsonb_object_agg(reason,n) FROM (SELECT reason,count(*) n FROM import_row,
            unnest(reasons) reason WHERE workspace_id=w AND source_id=s.source_id AND batch_id=s.batch_id GROUP BY reason) g))
    FROM live_job(w,r,t) j JOIN workspace ws ON ws.id=w JOIN snapshot s ON s.workspace_id=w AND s.id=j.snapshot_id
    JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,s.source_id,s.batch_id)
$$;

CREATE FUNCTION job_versions(w uuid,r uuid,t uuid,after_id uuid) RETURNS SETOF jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE snap uuid;
BEGIN
    SELECT j.snapshot_id INTO snap FROM live_job(w,r,t) j;
    IF NOT FOUND THEN RETURN; END IF;
    RETURN QUERY WITH page AS MATERIALIZED (
        SELECT m.source_id,m.version_id FROM snapshot_member m WHERE m.workspace_id=w AND m.snapshot_id=snap
            AND (after_id IS NULL OR m.version_id>after_id) ORDER BY m.version_id LIMIT 64
    ) SELECT to_jsonb(x) FROM (
        SELECT v.id AS version_id,i.id AS identity_id,i.external_place_id AS source_place_id,
            i.external_review_id AS source_review_id,v.version_no,v.normalization_version AS normalization,
            v.source_reviewer_id AS reviewer_id,v.text_content AS text,v.rating AS rating_1_to_5,v.metadata,v.content_sha256,
            to_char(v.event_date,'YYYY-MM-DD') AS event_date,
            to_char(v.event_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS event_utc,
            to_char(v.published_date,'YYYY-MM-DD') AS published_date,
            to_char(v.published_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS published_utc
        FROM page m
        JOIN LATERAL (SELECT * FROM review_version WHERE (workspace_id,source_id,id)=(w,m.source_id,m.version_id)
            LIMIT 1) v ON true
        JOIN LATERAL (SELECT * FROM review_identity WHERE (workspace_id,source_id,id)=(w,m.source_id,v.identity_id)
            LIMIT 1) i ON true
    ) x ORDER BY x.version_id;
END
$$;

REVOKE ALL ON FUNCTION job_writer(uuid),job_authorized(analysis_run),stop_attempt(analysis_run,text),
    live_job(uuid,uuid,uuid),enqueue_job(uuid,uuid,uuid,text,text),cancel_job(uuid,uuid),claim_job(),
    job_input(uuid,uuid,uuid),job_versions(uuid,uuid,uuid,uuid),
    worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION enqueue_job(uuid,uuid,uuid,text,text),cancel_job(uuid,uuid) TO rs_runtime;
GRANT USAGE ON SCHEMA public TO rs_worker;
GRANT SELECT(version_num) ON alembic_version TO rs_worker;
GRANT EXECUTE ON FUNCTION claim_job(),job_input(uuid,uuid,uuid),job_versions(uuid,uuid,uuid,uuid),
    worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) TO rs_worker;
GRANT SELECT(job_kind,engine_version,definition_version,model_version,attempt,stage,processed,total,reason,
    cancel_requested,finished_at,output_artifact_id) ON analysis_run TO rs_runtime;
GRANT SELECT(workspace_id,run_id,id,checksum,size_bytes,storage_state) ON artifact TO rs_runtime;
CREATE POLICY published_metadata ON artifact FOR SELECT TO rs_runtime USING (storage_state='available'
    AND EXISTS (SELECT FROM analysis_run j WHERE j.workspace_id=artifact.workspace_id AND j.id=artifact.run_id
        AND j.stage IS NOT NULL AND j.status='succeeded' AND j.output_artifact_id=artifact.id));
