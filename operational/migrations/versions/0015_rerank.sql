-- Derivatives live in the same source/snapshot retention unit as their frozen parent.
DO $$
DECLARE existing name;
BEGIN
 SELECT conname INTO STRICT existing FROM pg_constraint WHERE conrelid='analysis_run'::regclass
 AND contype='c' AND pg_get_constraintdef(oid) LIKE '%snapshot_candidate_search_v1%candidate_params IS NOT NULL%';
 EXECUTE format('ALTER TABLE analysis_run DROP CONSTRAINT %I',existing);
END $$;
ALTER TABLE analysis_run ADD CHECK (
 (coalesce(job_kind IN ('snapshot_candidate_search_v1','snapshot_rerank_v1'),false)) = (candidate_params IS NOT NULL)),
 ADD COLUMN rerank_parent uuid GENERATED ALWAYS AS ((candidate_params->>'parent_run_id')::uuid) STORED,
 ADD CHECK ((job_kind IS NOT DISTINCT FROM 'snapshot_rerank_v1') = coalesce(candidate_params ? 'parent_run_id',false)),
 ADD FOREIGN KEY(workspace_id,rerank_parent) REFERENCES analysis_run(workspace_id,id) DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION enqueue_rerank(w uuid,src uuid,s uuid,parent uuid,k uuid,profile text)
RETURNS TABLE(code integer,run_id uuid) LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE permitted integer:=job_writer(w); params jsonb; archived boolean; prior analysis_run;
BEGIN
 IF permitted<>200 THEN RETURN QUERY SELECT permitted,NULL::uuid; RETURN; END IF;
 IF profile IS DISTINCT FROM 'mmarco-context-logit-v1' OR k IS NULL THEN RETURN QUERY SELECT 422,NULL::uuid; RETURN; END IF;
 IF NOT candidate_scope(w,src,s) THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
 SELECT j.candidate_params || jsonb_build_object('parent_run_id',parent,'parent_sha256',art.checksum,'profile',profile,
    'session_binding',encode(sha256(x.token_hash||uuid_send(k)),'hex')),h.archived_at IS NOT NULL INTO params,archived
 FROM analysis_run j JOIN artifact art ON (art.workspace_id,art.run_id,art.id)=(w,parent,j.output_artifact_id)
 JOIN aspect h ON (h.workspace_id,h.project_id,h.aspect_id)=(w,j.bound_project,j.bound_aspect)
 JOIN access_session x ON x.principal_id=access_actor() AND x.token_hash=sha256(convert_to(current_setting('rs.session_proof',true),'UTF8'))
 WHERE (j.workspace_id,j.bound_source,j.snapshot_id,j.id)=(w,src,s,parent) AND j.status='succeeded'
 AND j.job_kind='snapshot_candidate_search_v1' AND art.storage_state='available' AND art.size_bytes BETWEEN 1 AND 65536
 AND aspect_scope(w,j.bound_project) FOR SHARE OF j,art,h,x;
 IF NOT FOUND THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
 SELECT * INTO prior FROM analysis_run WHERE workspace_id=w AND request_key=k;
 IF FOUND THEN
   IF (prior.snapshot_id,prior.candidate_params,prior.requested_by) IS DISTINCT FROM (s,params,access_actor())
   THEN RETURN QUERY SELECT 409,NULL::uuid; ELSE RETURN QUERY SELECT 200,prior.id; END IF;
   RETURN;
 END IF;
 IF archived THEN RETURN QUERY SELECT 409,NULL::uuid; RETURN; END IF;
 IF (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND status IN ('queued','running'))>=4 OR
    (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND created_at>clock_timestamp()-interval '1 hour')>=20
 THEN RETURN QUERY SELECT 429,NULL::uuid; RETURN; END IF;
 INSERT INTO analysis_run(workspace_id,snapshot_id,job_kind,request_key,requested_by,engine_version,definition_version,
   model_version,stage,ready_at,candidate_params) VALUES(w,s,'snapshot_rerank_v1',k,access_actor(),'1','candidate-rerank-1',
   profile,'queued',clock_timestamp(),params) RETURNING id INTO run_id;
 code:=202; RETURN NEXT;
END $$;
REVOKE ALL ON FUNCTION enqueue_rerank(uuid,uuid,uuid,uuid,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION enqueue_rerank(uuid,uuid,uuid,uuid,uuid,text) TO rs_runtime;

CREATE FUNCTION rerank_parent_valid(j analysis_run) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM analysis_run p JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=(p.workspace_id,p.id,p.output_artifact_id)
 WHERE (p.workspace_id,p.snapshot_id,p.bound_source,p.id)=(j.workspace_id,j.snapshot_id,j.bound_source,j.rerank_parent)
 AND p.job_kind='snapshot_candidate_search_v1' AND p.status='succeeded' AND a.storage_state='available'
 AND a.checksum=j.candidate_params->>'parent_sha256' AND p.candidate_params=j.candidate_params-ARRAY['parent_run_id','parent_sha256','profile','session_binding'])
$$;
REVOKE ALL ON FUNCTION rerank_parent_valid(analysis_run) FROM PUBLIC;
ALTER FUNCTION job_authorized(analysis_run) RENAME TO pre_rerank_job_authorized;
CREATE FUNCTION job_authorized(j analysis_run) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT pre_rerank_job_authorized(j) AND (j.job_kind IS DISTINCT FROM 'snapshot_rerank_v1' OR
   candidate_admitted(j.workspace_id,j.snapshot_id) AND rerank_parent_valid(j) AND EXISTS(
    SELECT FROM access_session x WHERE x.principal_id=j.requested_by AND x.revoked_at IS NULL AND x.expires_at>statement_timestamp()
    AND encode(sha256(x.token_hash||uuid_send(j.request_key)),'hex')=j.candidate_params->>'session_binding'))
$$;
REVOKE ALL ON FUNCTION job_authorized(analysis_run) FROM PUBLIC;
CREATE POLICY rerank_export ON analysis_run AS RESTRICTIVE FOR SELECT TO rs_retention
 USING(job_kind IS DISTINCT FROM 'snapshot_rerank_v1' OR candidate_exportable(workspace_id,snapshot_id));

CREATE FUNCTION rerank_authority(j analysis_run) RETURNS boolean LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 -- Lock mutable authority through COMMIT, including the exact initiating session.
 PERFORM FROM access_session x JOIN principal u ON u.id=x.principal_id
 JOIN workspace_membership m ON (m.principal_id,m.workspace_id)=(u.id,j.workspace_id)
 JOIN snapshot s ON (s.workspace_id,s.id)=(j.workspace_id,j.snapshot_id)
 JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(s.workspace_id,s.source_id,s.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(s.workspace_id,s.source_id,b.file_sha256)
 JOIN artifact art ON (art.workspace_id,art.run_id,art.checksum)=(s.workspace_id,j.rerank_parent,j.candidate_params->>'parent_sha256')
 WHERE u.id=j.requested_by AND m.state='active' AND m.role IN ('owner','analyst') AND u.state='active'
 AND x.revoked_at IS NULL AND x.expires_at>clock_timestamp() AND a.evidence_allowed AND a.candidate_allowed
 AND art.storage_state='available' AND encode(sha256(x.token_hash||uuid_send(j.request_key)),'hex')=j.candidate_params->>'session_binding'
 FOR SHARE OF x,u,m,a,art;
 RETURN FOUND AND job_authorized(j);
END $$;
REVOKE ALL ON FUNCTION rerank_authority(analysis_run) FROM PUBLIC;

ALTER FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) RENAME TO pre_rerank_step;
REVOKE ALL ON FUNCTION pre_rerank_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM rs_worker;
CREATE FUNCTION worker_step(w uuid,r uuid,t uuid,next_stage text,n integer,den integer,
 problem text DEFAULT NULL,digest sha256_hex DEFAULT NULL,bytes integer DEFAULT NULL,manifest sha256_hex DEFAULT NULL)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run;
BEGIN
 IF NOT coalesce(retention_online(),false) THEN RETURN 'stale'; END IF;
 SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t) AND job_kind='snapshot_rerank_v1';
 IF FOUND THEN
   PERFORM FROM workspace WHERE id=w FOR SHARE;
   PERFORM FROM client_project WHERE (workspace_id,id)=(w,j.bound_project) FOR UPDATE;
   IF NOT rerank_authority(j) THEN
     SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t)
       AND status='running' AND lease_until>clock_timestamp() FOR UPDATE;
     IF NOT FOUND THEN RETURN 'stale'; END IF;
     RETURN stop_attempt(j,CASE WHEN j.cancel_requested THEN 'CANCELLED' ELSE 'ACCESS_REVOKED' END);
   END IF;
 END IF;
 RETURN pre_rerank_step(w,r,t,next_stage,n,den,problem,digest,bytes,manifest);
END $$;
REVOKE ALL ON FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) TO rs_worker;

CREATE FUNCTION verify_rerank_publication() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run; raw text; base text; doc jsonb; parent jsonb;
BEGIN
 SELECT * INTO j FROM analysis_run WHERE (workspace_id,id)=(NEW.workspace_id,NEW.run_id);
 IF j.job_kind IS DISTINCT FROM 'snapshot_rerank_v1' THEN RETURN NEW; END IF;
 IF NOT rerank_authority(j) THEN RAISE EXCEPTION 'rerank_denied'; END IF;
 raw:=current_setting('rs.candidate_projection',true); base:=current_setting('rs.rerank_parent',true);
 IF session_user<>'rs_worker' OR raw IS NULL OR base IS NULL OR octet_length(raw)<>NEW.size_bytes
 OR encode(sha256(convert_to(raw,'UTF8')),'hex')<>NEW.checksum
 OR encode(sha256(convert_to(base,'UTF8')),'hex') IS DISTINCT FROM j.candidate_params->>'parent_sha256'
 THEN RAISE EXCEPTION 'invalid_rerank_artifact'; END IF;
 doc:=raw::jsonb; parent:=base::jsonb;
 IF (doc->>'schema',doc->>'run_id',doc->>'parent_run_id',doc->>'parent_sha256',doc->>'parent_result_sha256',doc->>'profile',
 doc->>'workspace_id',doc->>'source_id',doc->>'snapshot_id',doc->>'manifest_sha256',doc->>'query_sha256',doc->'aspect',parent->>'status')
 IS DISTINCT FROM ('candidate-rerank-1',j.id::text,j.rerank_parent::text,j.candidate_params->>'parent_sha256',parent->>'result_sha256',j.model_version,
 j.workspace_id::text,j.bound_source::text,j.snapshot_id::text,parent->>'manifest_sha256',parent->>'query_sha256',parent->'aspect','READY')
 OR jsonb_typeof(doc->'ranking') IS DISTINCT FROM 'array' OR jsonb_array_length(doc->'ranking')>40
 THEN RAISE EXCEPTION 'invalid_rerank_scope'; END IF;
 IF doc->>'status'='READY' THEN
   IF (SELECT array_agg(v->>'fragment_id' ORDER BY v->>'fragment_id') FROM jsonb_array_elements(doc->'ranking') v)
   IS DISTINCT FROM (SELECT array_agg(v->>'fragment_id' ORDER BY v->>'fragment_id') FROM jsonb_array_elements(parent->'candidates') v)
   THEN RAISE EXCEPTION 'invalid_rerank_pool'; END IF;
   IF EXISTS (WITH ranked AS (
      SELECT h,ord,b,(h->>'score')::double precision AS score FROM
      jsonb_array_elements(doc->'ranking') WITH ORDINALITY hits(h,ord)
      JOIN jsonb_array_elements(parent->'candidates') b ON b->>'fragment_id'=h->>'fragment_id'),
    first_hit AS (SELECT *,min(ord) OVER (PARTITION BY b->>'version_id') AS first_ord,
      row_number() OVER (ORDER BY score DESC,h->>'fragment_id' COLLATE "C") AS expected FROM ranked),
    checked AS (SELECT *,dense_rank() OVER (ORDER BY first_ord) AS review FROM first_hit)
    SELECT FROM checked WHERE (jsonb_typeof(h->'score'),h->>'rank',h->>'pre_rank',h->>'review_rank',ord)
    IS DISTINCT FROM ('number',ord::text,b->>'rank',review::text,expected))
   THEN RAISE EXCEPTION 'invalid_rerank_order'; END IF;
 ELSIF doc->>'status' IS DISTINCT FROM 'BASELINE_RETAINED' OR jsonb_array_length(doc->'ranking')<>0 THEN
   RAISE EXCEPTION 'invalid_rerank_fallback';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION verify_rerank_publication() FROM PUBLIC;
CREATE TRIGGER verify_rerank_publication BEFORE INSERT ON artifact FOR EACH ROW EXECUTE FUNCTION verify_rerank_publication();


CREATE FUNCTION rerank_object(w uuid,src uuid,s uuid,r uuid,actor uuid,expires bigint,ticket text)
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
 AND candidate_admitted(w,s) AND aspect_project_allowed(w,j.bound_project) AND rerank_parent_valid(j) AND j.status='succeeded' AND j.job_kind='snapshot_rerank_v1'
 AND (j.engine_version,j.definition_version)=('1','candidate-rerank-1')
 AND a.storage_state='available' AND a.size_bytes BETWEEN 1 AND 65536
 AND expires BETWEEN floor(extract(epoch FROM statement_timestamp()))::bigint
 AND floor(extract(epoch FROM statement_timestamp()))::bigint+30
 AND EXISTS(SELECT FROM access_session x WHERE x.principal_id=actor AND x.revoked_at IS NULL
 AND x.expires_at>statement_timestamp() AND encode(sha256(x.token_hash || sha256(convert_to(
 concat_ws('|','candidate-rerank-1',w,src,s,r,actor,expires),'UTF8'))),'hex')=ticket)
$$;
REVOKE ALL ON FUNCTION rerank_object(uuid,uuid,uuid,uuid,uuid,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rerank_object(uuid,uuid,uuid,uuid,uuid,bigint,text) TO rs_upload;

CREATE FUNCTION rerank_visible(w uuid,src uuid,s uuid,parent uuid,r uuid) RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT candidate_scope(w,src,s) AND EXISTS(SELECT FROM analysis_run j
 WHERE (j.workspace_id,j.bound_source,j.snapshot_id,j.rerank_parent,j.id)=(w,src,s,parent,r)
 AND aspect_scope(w,j.bound_project) AND rerank_parent_valid(j))
$$;
REVOKE ALL ON FUNCTION rerank_visible(uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rerank_visible(uuid,uuid,uuid,uuid,uuid) TO rs_runtime;
