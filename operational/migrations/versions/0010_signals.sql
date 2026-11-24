-- Use the same byte-specific intake and evidence grant as canonical review detail.
CREATE FUNCTION signal_admitted(w uuid,k uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM snapshot s JOIN import_batch b ON
 (b.workspace_id,b.source_id,b.id)=(w,s.source_id,s.batch_id)
 JOIN source src ON (src.workspace_id,src.id)=(w,s.source_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,s.source_id,b.file_sha256)
 WHERE (s.workspace_id,s.id)=(w,k) AND src.kind='synthetic_demo' AND a.evidence_allowed
 AND s.filters->>'format'='canonical-json-v1')
$$;
REVOKE ALL ON FUNCTION signal_admitted(uuid,uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION enqueue_job(w uuid,s uuid,k uuid,kind text,version text)
RETURNS TABLE(code integer,run_id uuid) LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE prior analysis_run; permitted integer := job_writer(w);
BEGIN
    IF permitted<>200 THEN RETURN QUERY SELECT permitted,NULL::uuid; RETURN; END IF;
    IF NOT EXISTS (SELECT FROM snapshot WHERE workspace_id=w AND id=s)
        THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
    IF kind='snapshot_explainable_signals_v1' AND NOT signal_admitted(w,s)
        THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
    SELECT * INTO prior FROM analysis_run WHERE workspace_id=w AND request_key=k;
    IF FOUND THEN
        IF (prior.snapshot_id,prior.job_kind,prior.engine_version) IS DISTINCT FROM (s,kind,version)
            THEN RETURN QUERY SELECT 409,NULL::uuid;
        ELSE RETURN QUERY SELECT 200,prior.id; END IF;
        RETURN;
    END IF;
    IF k IS NULL OR kind IS NULL OR kind NOT IN ('snapshot_integrity_and_summary_v1','snapshot_explainable_signals_v1') OR version IS DISTINCT FROM '1'
        THEN RETURN QUERY SELECT 422,NULL::uuid; RETURN; END IF;
    IF (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL
        AND status IN ('queued','running'))>=4 OR
       (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND
        created_at>clock_timestamp()-interval '1 hour')>=20 THEN RETURN QUERY SELECT 429,NULL::uuid; RETURN; END IF;
    INSERT INTO analysis_run(workspace_id,snapshot_id,job_kind,request_key,requested_by,engine_version,
        definition_version,model_version,stage,ready_at) VALUES
        (w,s,kind,k,access_actor(),version,CASE kind WHEN 'snapshot_explainable_signals_v1' THEN 'signals-1' ELSE 'summary-1' END,'unused','queued',clock_timestamp())
        RETURNING id INTO run_id;
    code:=202; RETURN NEXT;
END $$;


ALTER FUNCTION job_input(uuid,uuid,uuid) RENAME TO verified_job_input;
REVOKE ALL ON FUNCTION verified_job_input(uuid,uuid,uuid) FROM rs_worker;
CREATE FUNCTION job_input(w uuid,r uuid,t uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT verified_job_input(w,r,t) || CASE WHEN j.job_kind='snapshot_explainable_signals_v1'
 THEN jsonb_build_object('as_of',(j.created_at AT TIME ZONE 'UTC')::date) ELSE '{}'::jsonb END
 FROM live_job(w,r,t) j
$$;
REVOKE ALL ON FUNCTION job_input(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION job_input(uuid,uuid,uuid) TO rs_worker;

ALTER FUNCTION job_authorized(analysis_run) RENAME TO authorized_requester;
CREATE FUNCTION job_authorized(j analysis_run) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT authorized_requester(j) AND (j.job_kind<>'snapshot_explainable_signals_v1'
 OR signal_admitted(j.workspace_id,j.snapshot_id))
$$;
REVOKE ALL ON FUNCTION job_authorized(analysis_run) FROM PUBLIC;

CREATE OR REPLACE FUNCTION worker_step(w uuid,r uuid,t uuid,next_stage text,n integer,den integer,
 problem text DEFAULT NULL,digest sha256_hex DEFAULT NULL,bytes integer DEFAULT NULL,manifest sha256_hex DEFAULT NULL)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run;
BEGIN
 IF NOT coalesce(retention_online(),false) THEN RETURN 'stale'; END IF;
 SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t);
 IF FOUND AND j.job_kind='snapshot_explainable_signals_v1' THEN
   -- Hold the grant through publication so a concurrent revocation linearizes.
   PERFORM FROM browser_admission a JOIN import_batch b ON
   (b.workspace_id,b.source_id,b.file_sha256)=(a.workspace_id,a.source_id,a.checksum)
   JOIN snapshot s ON (s.workspace_id,s.source_id,s.batch_id)=(w,b.source_id,b.id)
   JOIN source src ON (src.workspace_id,src.id)=(w,s.source_id)
   WHERE a.workspace_id=w AND s.id=j.snapshot_id AND a.evidence_allowed
   AND src.kind='synthetic_demo' FOR SHARE OF a,src;
   IF NOT FOUND THEN
     SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t)
       AND status='running' AND lease_until>clock_timestamp() FOR UPDATE;
     IF NOT FOUND THEN RETURN 'stale'; END IF;
     RETURN stop_attempt(j,'ACCESS_REVOKED');
   END IF;
 END IF;
 RETURN step_ready_job(w,r,t,next_stage,n,den,problem,digest,bytes,manifest);
END $$;

-- The importer receives a short-lived proof for one immutable result. It never receives
-- a login cookie, session proof, arbitrary object key or caller-supplied filesystem path.
CREATE FUNCTION signal_object(w uuid,src uuid,s uuid,r uuid,actor uuid,expires bigint,ticket text)
RETURNS TABLE(root text,digest text,bytes bigint,manifest text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT ws.delivery_object_root,a.checksum::text,a.size_bytes,sn.manifest_sha256::text
 FROM workspace ws JOIN snapshot sn ON (sn.workspace_id,sn.source_id,sn.id)=(w,src,s)
 JOIN analysis_run j ON (j.workspace_id,j.snapshot_id,j.id)=(w,s,r)
 JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=(w,r,j.output_artifact_id)
 JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(w,actor)
 JOIN principal p ON p.id=actor
 WHERE session_user='rs_upload' AND retention_online() AND ws.id=w AND ws.state='active'
 AND m.state='active' AND m.role IN ('owner','analyst') AND p.state='active'
 AND signal_admitted(w,s) AND j.status='succeeded' AND j.job_kind='snapshot_explainable_signals_v1'
 AND (j.engine_version,j.definition_version,j.model_version)=('1','signals-1','unused')
 AND a.storage_state='available' AND a.size_bytes BETWEEN 1 AND 65536
 AND expires BETWEEN floor(extract(epoch FROM statement_timestamp()))::bigint
 AND floor(extract(epoch FROM statement_timestamp()))::bigint+30
 AND EXISTS(SELECT FROM access_session x WHERE x.principal_id=actor AND x.revoked_at IS NULL
 AND x.expires_at>statement_timestamp() AND encode(sha256(x.token_hash || sha256(convert_to(
 concat_ws('|','signals-1',w,src,s,r,actor,expires),'UTF8'))),'hex')=ticket)
$$;
REVOKE ALL ON FUNCTION signal_object(uuid,uuid,uuid,uuid,uuid,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION signal_object(uuid,uuid,uuid,uuid,uuid,bigint,text) TO rs_upload;

CREATE FUNCTION check_signal_publication() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run; sn snapshot; raw text; doc jsonb;
BEGIN
 SELECT * INTO j FROM analysis_run WHERE (workspace_id,id)=(NEW.workspace_id,NEW.run_id);
 IF j.job_kind IS DISTINCT FROM 'snapshot_explainable_signals_v1' THEN RETURN NEW; END IF;
 SELECT * INTO STRICT sn FROM snapshot WHERE (workspace_id,id)=(j.workspace_id,j.snapshot_id);
 raw:=current_setting('rs.signals_projection',true);
 IF session_user<>'rs_worker' OR raw IS NULL OR octet_length(raw)<>NEW.size_bytes
 OR encode(sha256(convert_to(raw,'UTF8')),'hex')<>NEW.checksum THEN RAISE EXCEPTION 'invalid_signal_artifact'; END IF;
 doc:=raw::jsonb;
 IF (doc->>'schema',doc->>'workspace_id',doc->>'run_id',doc->>'snapshot_id',
 doc->>'source_id',doc->>'manifest_sha256',doc#>>'{parameters,as_of}')
 IS DISTINCT FROM ('snapshot-explainable-signals-1',j.workspace_id::text,j.id::text,sn.id::text,
 sn.source_id::text,sn.manifest_sha256::text,((j.created_at AT TIME ZONE 'UTC')::date)::text)
 THEN RAISE EXCEPTION 'invalid_signal_scope'; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION check_signal_publication() FROM PUBLIC;
CREATE TRIGGER check_signal_publication BEFORE INSERT ON artifact FOR EACH ROW EXECUTE FUNCTION check_signal_publication();

CREATE FUNCTION signal_scope(w uuid,src uuid,s uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT current_setting('rs.workspace',true)=w::text AND browser_member(w,src) AND signal_admitted(w,s)
 AND EXISTS(SELECT FROM snapshot WHERE (workspace_id,source_id,id)=(w,src,s))
$$;
REVOKE ALL ON FUNCTION signal_scope(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION signal_scope(uuid,uuid,uuid) TO rs_runtime;
