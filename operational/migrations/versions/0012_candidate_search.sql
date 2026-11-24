-- Byte-specific opt-in for a synthetic source. The PR-13 access seal hashes the full admission row.
ALTER TABLE browser_admission ADD COLUMN candidate_allowed boolean NOT NULL DEFAULT false;
ALTER TABLE analysis_run ADD COLUMN candidate_params jsonb;
ALTER TABLE analysis_run ADD CHECK ((job_kind IS NOT DISTINCT FROM 'snapshot_candidate_search_v1') =
                                    (candidate_params IS NOT NULL));
CREATE FUNCTION immutable_candidate_params() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.candidate_params IS DISTINCT FROM OLD.candidate_params THEN RAISE EXCEPTION 'immutable_request'; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION immutable_candidate_params() FROM PUBLIC;
CREATE TRIGGER immutable_candidate_params BEFORE UPDATE ON analysis_run FOR EACH ROW
 EXECUTE FUNCTION immutable_candidate_params();

CREATE FUNCTION candidate_admitted(w uuid,s uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT signal_admitted(w,s) AND EXISTS(SELECT FROM snapshot sn JOIN import_batch b ON
 (b.workspace_id,b.source_id,b.id)=(w,sn.source_id,sn.batch_id) JOIN browser_admission a
 ON (a.workspace_id,a.source_id,a.checksum)=(w,b.source_id,b.file_sha256)
 WHERE (sn.workspace_id,sn.id)=(w,s) AND a.candidate_allowed)
$$;
REVOKE ALL ON FUNCTION candidate_admitted(uuid,uuid) FROM PUBLIC;

CREATE FUNCTION candidate_scope(w uuid,src uuid,s uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT signal_scope(w,src,s) THEN RETURN false; END IF;
 PERFORM FROM snapshot sn JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,src,b.file_sha256)
 JOIN source z ON (z.workspace_id,z.id)=(w,src)
 JOIN workspace ws ON ws.id=w JOIN workspace_membership m ON m.workspace_id=w
 JOIN principal p ON p.id=m.principal_id JOIN access_session x ON x.principal_id=p.id
 WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s) AND ws.state='active' AND z.kind='synthetic_demo'
 AND m.state='active' AND m.role IN ('owner','analyst') AND p.state='active'
 AND a.evidence_allowed AND a.candidate_allowed AND x.revoked_at IS NULL AND x.expires_at>statement_timestamp()
 AND x.token_hash=sha256(convert_to(nullif(current_setting('rs.session_proof',true),''),'UTF8'))
 FOR SHARE OF ws,z,m,p,x,a;
 RETURN FOUND;
END
$$;
REVOKE ALL ON FUNCTION candidate_scope(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION candidate_scope(uuid,uuid,uuid) TO rs_runtime;

CREATE FUNCTION enqueue_candidate(w uuid,src uuid,s uuid,k uuid,q text,m text,role_name text,top_k integer)
RETURNS TABLE(code integer,run_id uuid) LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE prior analysis_run; permitted integer := job_writer(w); params jsonb;
BEGIN
 IF permitted<>200 THEN RETURN QUERY SELECT permitted,NULL::uuid; RETURN; END IF;
 IF NOT candidate_scope(w,src,s) THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
 IF num_nulls(k,q,m,role_name,top_k)>0 OR octet_length(q) NOT BETWEEN 1 AND 2048 OR q !~ '[^[:space:]]'
 OR m NOT IN ('lexical','dense','hybrid') OR role_name NOT IN ('query_to_passage','text_to_text')
 OR top_k NOT BETWEEN 1 AND 10 THEN RETURN QUERY SELECT 422,NULL::uuid; RETURN; END IF;
 params:=jsonb_build_object('query',q,'mode',m,'role',role_name,'k',top_k);
 SELECT * INTO prior FROM analysis_run WHERE workspace_id=w AND request_key=k;
 IF FOUND THEN
   IF (prior.snapshot_id,prior.candidate_params,prior.requested_by) IS DISTINCT FROM (s,params,access_actor())
   THEN RETURN QUERY SELECT 409,NULL::uuid; ELSE RETURN QUERY SELECT 200,prior.id; END IF;
   RETURN;
 END IF;
 IF (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND status IN ('queued','running'))>=4 OR
    (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND created_at>clock_timestamp()-interval '1 hour')>=20
 THEN RETURN QUERY SELECT 429,NULL::uuid; RETURN; END IF;
 INSERT INTO analysis_run(workspace_id,snapshot_id,job_kind,request_key,requested_by,engine_version,definition_version,
    model_version,stage,ready_at,candidate_params) VALUES(w,s,'snapshot_candidate_search_v1',k,access_actor(),'1',
    'candidate-search-1',CASE WHEN m='lexical' THEN 'unused' ELSE 'minilm-pr04' END,'queued',clock_timestamp(),params)
 RETURNING id INTO run_id;
 code:=202; RETURN NEXT;
END $$;
REVOKE ALL ON FUNCTION enqueue_candidate(uuid,uuid,uuid,uuid,text,text,text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION enqueue_candidate(uuid,uuid,uuid,uuid,text,text,text,integer) TO rs_runtime;

ALTER FUNCTION job_input(uuid,uuid,uuid) RENAME TO pre_candidate_input;
REVOKE ALL ON FUNCTION pre_candidate_input(uuid,uuid,uuid) FROM rs_worker;
CREATE FUNCTION job_input(w uuid,r uuid,t uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT base.input || CASE WHEN j.candidate_params IS NULL THEN '{}'::jsonb
 ELSE jsonb_build_object('candidate',j.candidate_params,'valid',(base.input->>'valid')::boolean AND
   (SELECT sn.version_ids IS NOT DISTINCT FROM array(
      SELECT ir.version_id FROM import_row ir WHERE (ir.workspace_id,ir.source_id,ir.batch_id)=(w,sn.source_id,sn.batch_id)
      AND ir.status='accepted' ORDER BY ir.version_id)
    FROM snapshot sn WHERE (sn.workspace_id,sn.id)=(w,j.snapshot_id))) END
 FROM live_job(w,r,t) j CROSS JOIN LATERAL (SELECT pre_candidate_input(w,r,t) AS input OFFSET 0) base
$$;
REVOKE ALL ON FUNCTION job_input(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION job_input(uuid,uuid,uuid) TO rs_worker;

CREATE OR REPLACE FUNCTION job_authorized(j analysis_run) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT authorized_requester(j) AND CASE j.job_kind
 WHEN 'snapshot_candidate_search_v1' THEN candidate_admitted(j.workspace_id,j.snapshot_id)
 WHEN 'snapshot_explainable_signals_v1' THEN signal_admitted(j.workspace_id,j.snapshot_id)
 ELSE true END
$$;

-- Restrict owner exports of candidate parameters and their artifacts to a live processing grant.
CREATE FUNCTION candidate_exportable(w uuid,s uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM snapshot sn WHERE (sn.workspace_id,sn.id)=(w,s) AND (w,sn.source_id)
 IN (SELECT * FROM retention_export_sources())) AND candidate_admitted(w,s)
$$;
REVOKE ALL ON FUNCTION candidate_exportable(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION candidate_exportable(uuid,uuid) TO rs_retention;
CREATE POLICY candidate_export ON analysis_run AS RESTRICTIVE FOR SELECT TO rs_retention
 USING(job_kind IS DISTINCT FROM 'snapshot_candidate_search_v1' OR candidate_exportable(workspace_id,snapshot_id));
GRANT SELECT(candidate_params) ON analysis_run TO rs_retention;

ALTER FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) RENAME TO pre_candidate_step;
REVOKE ALL ON FUNCTION pre_candidate_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM rs_worker;
CREATE FUNCTION worker_step(w uuid,r uuid,t uuid,next_stage text,n integer,den integer,
 problem text DEFAULT NULL,digest sha256_hex DEFAULT NULL,bytes integer DEFAULT NULL,manifest sha256_hex DEFAULT NULL)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run;
BEGIN
 IF NOT coalesce(retention_online(),false) THEN RETURN 'stale'; END IF;
 SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t);
 IF FOUND AND j.job_kind='snapshot_candidate_search_v1' THEN
   -- Hold the grant through publication so a concurrent revocation linearizes.
   PERFORM FROM browser_admission a JOIN import_batch b ON
   (b.workspace_id,b.source_id,b.file_sha256)=(a.workspace_id,a.source_id,a.checksum)
   JOIN snapshot s ON (s.workspace_id,s.source_id,s.batch_id)=(w,b.source_id,b.id)
   JOIN source src ON (src.workspace_id,src.id)=(w,s.source_id)
   WHERE a.workspace_id=w AND s.id=j.snapshot_id AND a.evidence_allowed AND a.candidate_allowed
   AND src.kind='synthetic_demo' FOR SHARE OF a,src;
   IF NOT FOUND THEN
     SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t)
       AND status='running' AND lease_until>clock_timestamp() FOR UPDATE;
     IF NOT FOUND THEN RETURN 'stale'; END IF;
     RETURN stop_attempt(j,'ACCESS_REVOKED');
   END IF;
 END IF;
 RETURN pre_candidate_step(w,r,t,next_stage,n,den,problem,digest,bytes,manifest);
END $$;

REVOKE ALL ON FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) TO rs_worker;
CREATE FUNCTION candidate_object(w uuid,src uuid,s uuid,r uuid,actor uuid,expires bigint,ticket text)
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
 AND candidate_admitted(w,s) AND j.status='succeeded' AND j.job_kind='snapshot_candidate_search_v1'
 AND (j.engine_version,j.definition_version)=('1','candidate-search-1')
 AND a.storage_state='available' AND a.size_bytes BETWEEN 1 AND 65536
 AND expires BETWEEN floor(extract(epoch FROM statement_timestamp()))::bigint
 AND floor(extract(epoch FROM statement_timestamp()))::bigint+30
 AND EXISTS(SELECT FROM access_session x WHERE x.principal_id=actor AND x.revoked_at IS NULL
 AND x.expires_at>statement_timestamp() AND encode(sha256(x.token_hash || sha256(convert_to(
 concat_ws('|','candidate-search-1',w,src,s,r,actor,expires),'UTF8'))),'hex')=ticket)
$$;
REVOKE ALL ON FUNCTION candidate_object(uuid,uuid,uuid,uuid,uuid,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION candidate_object(uuid,uuid,uuid,uuid,uuid,bigint,text) TO rs_upload;

CREATE FUNCTION check_candidate_publication() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run; sn snapshot; raw text; doc jsonb; hit jsonb; vid uuid; original record;
BEGIN
 SELECT * INTO j FROM analysis_run WHERE (workspace_id,id)=(NEW.workspace_id,NEW.run_id);
 IF j.job_kind IS DISTINCT FROM 'snapshot_candidate_search_v1' THEN RETURN NEW; END IF;
 SELECT * INTO STRICT sn FROM snapshot WHERE (workspace_id,id)=(j.workspace_id,j.snapshot_id);
 raw:=current_setting('rs.candidate_projection',true);
 IF session_user<>'rs_worker' OR raw IS NULL OR octet_length(raw)<>NEW.size_bytes
 OR encode(sha256(convert_to(raw,'UTF8')),'hex')<>NEW.checksum THEN RAISE EXCEPTION 'invalid_candidate_artifact'; END IF;
 doc:=raw::jsonb;
 IF (doc->>'schema',doc->>'workspace_id',doc->>'run_id',doc->>'snapshot_id',
 doc->>'source_id',doc->>'manifest_sha256',doc->>'query_sha256',doc->>'mode',doc->>'role',doc->>'k')
 IS DISTINCT FROM ('candidate-search-1',j.workspace_id::text,j.id::text,sn.id::text,
 sn.source_id::text,sn.manifest_sha256::text,encode(sha256(convert_to(j.candidate_params->>'query','UTF8')),'hex'),j.candidate_params->>'mode',j.candidate_params->>'role',j.candidate_params->>'k')
 THEN RAISE EXCEPTION 'invalid_candidate_scope'; END IF;
 IF jsonb_typeof(doc->'candidates') IS DISTINCT FROM 'array'
 OR jsonb_array_length(doc->'candidates') IS DISTINCT FROM (doc->>'candidate_count')::int
 OR jsonb_array_length(doc->'candidates') > 4*(j.candidate_params->>'k')::int
 OR (doc->>'reviewed_count')::int IS DISTINCT FROM cardinality(sn.version_ids)
 THEN RAISE EXCEPTION 'invalid_candidate_counts'; END IF;
 -- At most 40 hits. Resolve their primary keys individually before fresh tables have statistics.
 FOR hit IN SELECT value FROM jsonb_array_elements(doc->'candidates') LOOP
   vid := (hit->>'version_id')::uuid;
   IF NOT EXISTS(SELECT FROM snapshot_member m
     WHERE (m.workspace_id,m.snapshot_id,m.version_id,m.source_id)=(sn.workspace_id,sn.id,vid,sn.source_id))
   THEN RAISE EXCEPTION 'invalid_candidate_span'; END IF;
   SELECT v.content_sha256,v.text_content INTO original FROM review_version v
     WHERE (v.workspace_id,v.source_id,v.id)=(sn.workspace_id,sn.source_id,vid);
   IF NOT FOUND OR original.content_sha256 IS DISTINCT FROM hit->>'content_sha256'
   OR encode(sha256(convert_to(original.text_content,'UTF8')),'hex') IS DISTINCT FROM hit->>'text_sha256'
   OR ((hit->>'context_start')::int>=0 AND (hit->>'context_start')::int<=(hit->>'start')::int
     AND (hit->>'start')::int<(hit->>'end')::int AND (hit->>'end')::int<=(hit->>'context_end')::int
     AND (hit->>'context_end')::int<=length(original.text_content)) IS NOT TRUE
   THEN RAISE EXCEPTION 'invalid_candidate_span'; END IF;
 END LOOP;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION check_candidate_publication() FROM PUBLIC;
CREATE TRIGGER check_candidate_publication BEFORE INSERT ON artifact FOR EACH ROW EXECUTE FUNCTION check_candidate_publication();
