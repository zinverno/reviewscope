CREATE OR REPLACE FUNCTION browser_claim(w uuid,s uuid,d uuid,h text,fp text)
 RETURNS TABLE(root text,source_key text,principal_id uuid,fmt text,locations uuid[]) LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 -- Check the exact ticket and scope before waiting, then recheck them after workspace SHARE.
 PERFORM FROM workspace ws WHERE ws.id=w AND ws.state='active' AND EXISTS (
   SELECT FROM browser_delivery x WHERE (x.workspace_id,x.source_id,x.id,x.checksum,x.fingerprint)=(w,s,d,h,fp)
   AND x.actor=browser_actor(w,s)) FOR SHARE OF ws;
 IF NOT FOUND THEN RETURN; END IF;
 RETURN QUERY SELECT ws.delivery_object_root::text,coalesce(src.external_source_id,src.source_key)::text,
 x.actor,x.fmt,x.locations FROM browser_delivery x JOIN workspace ws ON ws.id=x.workspace_id
 JOIN source src ON (src.workspace_id,src.id)=(w,s) JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,s,h)
 JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(w,x.actor)
 JOIN access_session ses ON (ses.token_hash,ses.principal_id)=(x.approval_session,x.actor)
 JOIN principal p ON p.id=x.actor WHERE (x.workspace_id,x.source_id,x.id,x.checksum,x.fingerprint)=(w,s,d,h,fp)
 AND x.actor=browser_actor(w,s)
 FOR UPDATE OF src,x FOR SHARE OF ws,m,ses,p,a;
END $$;

ALTER TABLE browser_admission ADD COLUMN monitor_allowed boolean NOT NULL DEFAULT false;
CREATE OR REPLACE FUNCTION retention_access_seal() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT CASE WHEN count(*)=0 THEN retention_access_seal_v6() ELSE encode(sha256(convert_to(retention_access_seal_v6() || string_agg(v,'' ORDER BY v),'UTF8')),'hex') END
 FROM (SELECT (to_jsonb(a) - CASE WHEN a.monitor_allowed THEN ARRAY[]::text[] ELSE ARRAY['monitor_allowed'] END
                       - CASE WHEN a.decision_allowed THEN ARRAY[]::text[] ELSE ARRAY['decision_allowed'] END
                       - CASE WHEN a.topic_discovery_allowed THEN ARRAY[]::text[] ELSE ARRAY['topic_discovery_allowed'] END
                       - CASE WHEN a.relation_allowed THEN ARRAY[]::text[] ELSE ARRAY['relation_allowed'] END
                       - CASE WHEN a.human_review_allowed THEN ARRAY[]::text[] ELSE ARRAY['human_review_allowed'] END)::text v
 FROM browser_admission a UNION ALL
 SELECT jsonb_build_array(sl.workspace_id,sl.source_id,sl.project_id,sl.location_id,sl.external_place_id,
 s.kind,s.source_key,s.external_source_id)::text FROM source_location sl JOIN source s
 ON (s.workspace_id,s.id)=(sl.workspace_id,sl.source_id)
 WHERE EXISTS(SELECT FROM browser_admission a WHERE (a.workspace_id,a.source_id)=(s.workspace_id,s.id))) grants
$$;

CREATE TABLE monitor (
 workspace_id uuid NOT NULL, project_id uuid NOT NULL, source_id uuid NOT NULL,
 monitor_id uuid NOT NULL DEFAULT gen_random_uuid(), created_by uuid NOT NULL REFERENCES principal,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(workspace_id,project_id,source_id,monitor_id), UNIQUE(monitor_id),
 FOREIGN KEY(workspace_id,project_id,source_id) REFERENCES source(workspace_id,project_id,id) ON DELETE CASCADE
);
CREATE TABLE monitor_version (
 workspace_id uuid NOT NULL, project_id uuid NOT NULL, source_id uuid NOT NULL, monitor_id uuid NOT NULL,
 version_id uuid NOT NULL DEFAULT gen_random_uuid(), version_no int NOT NULL CHECK(version_no>0),
 aspect_id uuid NOT NULL, aspect_version_id uuid NOT NULL, aspect_digest text NOT NULL,
 payload jsonb NOT NULL, digest text GENERATED ALWAYS AS (topic_hash(payload)) STORED,
 created_by uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(workspace_id,project_id,source_id,monitor_id,version_id), UNIQUE(version_id),
 UNIQUE(monitor_id,version_no),
 FOREIGN KEY(workspace_id,project_id,source_id,monitor_id) REFERENCES monitor ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,project_id,aspect_id,aspect_version_id,aspect_digest) REFERENCES aspect_version(workspace_id,project_id,aspect_id,version_id,digest)
);
CREATE TABLE monitor_archive (
 workspace_id uuid NOT NULL, project_id uuid NOT NULL, source_id uuid NOT NULL, monitor_id uuid NOT NULL,
 version_id uuid NOT NULL, created_by uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(monitor_id), FOREIGN KEY(workspace_id,project_id,source_id,monitor_id,version_id) REFERENCES monitor_version ON DELETE CASCADE
);
CREATE TABLE monitor_run (
 workspace_id uuid NOT NULL, project_id uuid NOT NULL, source_id uuid NOT NULL, monitor_id uuid NOT NULL,
 version_id uuid NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(), decision_id uuid NOT NULL,
 snapshot_id uuid NOT NULL, parent uuid, snapshot_sha sha256_hex NOT NULL, manifest_sha sha256_hex NOT NULL,
 file_sha sha256_hex NOT NULL, parent_artifact sha256_hex, parent_rows sha256_hex,
 created_by uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(id), UNIQUE(workspace_id,decision_id), UNIQUE(workspace_id,project_id,source_id,monitor_id,version_id,id),
 CHECK(num_nonnulls(parent,parent_artifact,parent_rows) IN (0,3)),
 FOREIGN KEY(workspace_id,project_id,source_id,monitor_id,version_id) REFERENCES monitor_version ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,decision_id,snapshot_id,source_id) REFERENCES decision_binding(workspace_id,run_id,snapshot_id,source_id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,project_id,source_id,monitor_id,version_id,parent) REFERENCES monitor_run(workspace_id,project_id,source_id,monitor_id,version_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX monitor_run_history ON monitor_run(monitor_id,created_at DESC,id);
CREATE INDEX monitor_run_target ON monitor_run(version_id,snapshot_id);
CREATE TABLE monitor_reuse (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, version_id uuid NOT NULL,
 parent_decision uuid NOT NULL, parent_row_sha sha256_hex NOT NULL,
 PRIMARY KEY(workspace_id,run_id,version_id),
 FOREIGN KEY(workspace_id,run_id) REFERENCES monitor_run(workspace_id,decision_id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,run_id,version_id) REFERENCES decision_row ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,parent_decision,version_id) REFERENCES decision_row DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE monitor_receipt (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, document jsonb NOT NULL,
 digest text GENERATED ALWAYS AS (topic_hash(document)) STORED,
 PRIMARY KEY(workspace_id,run_id), FOREIGN KEY(workspace_id,run_id) REFERENCES monitor_run(workspace_id,decision_id) ON DELETE CASCADE
);

CREATE FUNCTION monitor_scope(w uuid,p uuid,src uuid) RETURNS boolean LANGUAGE sql VOLATILE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT session_user='rs_runtime' AND browser_member(w,src) AND EXISTS(SELECT FROM source WHERE (workspace_id,project_id,id)=(w,p,src) AND kind='synthetic_demo')
 AND aspect_scope(w,p)
$$;
CREATE FUNCTION monitor_admitted(w uuid,src uuid,s uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT decision_admitted(w,s) AND EXISTS(SELECT FROM snapshot sn JOIN import_batch b ON
 (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id) JOIN browser_admission a ON
 (a.workspace_id,a.source_id,a.checksum)=(w,src,b.file_sha256) WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s)
 AND a.monitor_allowed AND b.status='accepted' AND sn.filters->>'selection'='accepted_in_delivery'
 AND sn.filters->>'format'='canonical-json-v1')
$$;

CREATE FUNCTION monitor_lineage(w uuid,r uuid) RETURNS TABLE(source_id uuid,snapshot_id uuid) LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 WITH RECURSIVE chain AS (
 SELECT m.id,m.parent,m.source_id,m.snapshot_id FROM monitor_run m WHERE (m.workspace_id,m.decision_id)=(w,r)
 UNION ALL SELECT m.id,m.parent,m.source_id,m.snapshot_id FROM chain c JOIN monitor_run m ON m.id=c.parent AND m.workspace_id=w
 ) SELECT c.source_id,c.snapshot_id FROM chain c
$$;
CREATE FUNCTION monitor_job_allowed(w uuid,r uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT NOT EXISTS(SELECT FROM monitor_lineage(w,r) l WHERE NOT monitor_admitted(w,l.source_id,l.snapshot_id))
$$;
CREATE FUNCTION monitor_lock_grants(w uuid,r uuid) RETURNS boolean LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM FROM monitor_lineage(w,r) c JOIN snapshot sn ON (sn.workspace_id,sn.source_id,sn.id)=(w,c.source_id,c.snapshot_id)
 JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,c.source_id,sn.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,c.source_id,b.file_sha256)
 ORDER BY a.source_id,a.checksum FOR SHARE OF a;
 RETURN monitor_job_allowed(w,r);
END $$;

-- Digests of the six frozen PR-22 manual definitions.
CREATE FUNCTION monitor_supported(origin text,digest text) RETURNS boolean LANGUAGE sql IMMUTABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT origin='manual' AND digest IN (
   'd4614d09b445e4a27bb2fd7690646ec6cfbe3aceddd3968e9c2d406710ad6848',
   '7f3c7010e46abc7e3e309feb0618f71c954e6eaab6b8904e5f299bc66e04ea48',
   '8f43d1180bfaddf25bc5081c67148970a8c401ca6399e5731b57dfdf35627901',
   '8dbb05a5e67b58ef45f680472fa72271e1823bb0489cd9a114cc3d667529f413',
   '5d9fd8836332bda21978c5ca2e4babb8e44732abf3a0415021e14376d8cbc92e',
   '2440217927739d5adbafa147f09936a26b8d90e8686245cec32a577a89605738')
$$;

CREATE FUNCTION monitor_save(w uuid,p uuid,src uuid,m uuid,expected uuid,a uuid,v uuid,title text,
 threshold_count int,min_eligible int,selection text,archive boolean) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE perm int:=job_writer(w); prior monitor_version; def aspect_version; saved uuid; spec jsonb; next_id uuid;
BEGIN
 IF perm<>200 OR NOT monitor_scope(w,p,src) THEN RETURN jsonb_build_object('code',404); END IF;
 IF (m IS NULL)<>(expected IS NULL) OR archive IS NULL THEN RETURN jsonb_build_object('code',422); END IF;
 IF m IS NOT NULL THEN
   SELECT * INTO prior FROM monitor_version WHERE (workspace_id,project_id,source_id,monitor_id)=(w,p,src,m) ORDER BY version_no DESC LIMIT 1;
   IF NOT FOUND THEN RETURN jsonb_build_object('code',404); END IF;
   IF prior.version_id<>expected OR EXISTS(SELECT FROM monitor_archive WHERE monitor_id=m) THEN RETURN jsonb_build_object('code',409); END IF;
 END IF;
 IF archive THEN
   IF m IS NULL OR num_nonnulls(a,v,title,threshold_count,min_eligible,selection)>0 THEN RETURN jsonb_build_object('code',422); END IF;
   INSERT INTO monitor_archive(workspace_id,project_id,source_id,monitor_id,version_id,created_by) VALUES(w,p,src,m,expected,access_actor());
   RETURN jsonb_build_object('code',200,'monitor_id',m,'version_id',expected);
 END IF;
 IF selection IS DISTINCT FROM 'accepted_in_delivery' OR coalesce(octet_length(title),0) NOT BETWEEN 1 AND 240
 OR NOT aspect_nonblank(title) OR coalesce(threshold_count,0) NOT BETWEEN 1 AND 10000 OR coalesce(min_eligible,0) NOT BETWEEN 1 AND 10000
 THEN RETURN jsonb_build_object('code',422); END IF;
 SELECT av.* INTO def FROM aspect_version av JOIN aspect h USING(workspace_id,project_id,aspect_id)
 WHERE (av.workspace_id,av.project_id,av.aspect_id,av.version_id)=(w,p,a,v) AND h.archived_at IS NULL FOR SHARE OF h;
 IF NOT FOUND THEN RETURN jsonb_build_object('code',404); END IF;
 IF m IS NULL THEN
   INSERT INTO monitor(workspace_id,project_id,source_id,created_by) VALUES(w,p,src,access_actor()) RETURNING monitor_id INTO saved;
 ELSE saved:=m; END IF;
 spec:=jsonb_build_object('schema','monitor-1','workspace',w,'project',p,'source',src,'name',title,'selection',selection,
 'aspect_id',a,'aspect_version_id',v,'aspect_digest',def.digest,'origin',def.origin,'supported',monitor_supported(def.origin,def.digest),
 'profile','aspect-linear-lexical-v1','threshold_set','calibrated-v1','profile_sha256',decision_profile_sha(),'aggregation','aggregate-1',
 'event',jsonb_build_object('rule','machine-mentioned-count-v1','threshold',threshold_count,'unit','reviews','minimum',min_eligible,
 'denominator','N_eligible','state','NOT_EVALUATED','reason','QUALITY_GATE_NOT_MET'));
 INSERT INTO monitor_version(workspace_id,project_id,source_id,monitor_id,version_no,aspect_id,aspect_version_id,aspect_digest,payload,created_by)
 VALUES(w,p,src,saved,coalesce(prior.version_no,0)+1,a,v,def.digest,spec,access_actor()) RETURNING version_id INTO next_id;
 RETURN jsonb_build_object('code',201,'monitor_id',saved,'version_id',next_id);
END $$;

CREATE FUNCTION monitor_start(w uuid,p uuid,src uuid,m uuid,v uuid,s uuid,parent_id uuid,k uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE perm int:=job_writer(w); mv monitor_version; old monitor_run; replay monitor_run; sn snapshot;
 b import_batch; pa artifact; pb decision_binding; job record; new_id uuid; ok boolean;
BEGIN
 IF perm<>200 OR NOT monitor_scope(w,p,src) OR NOT monitor_admitted(w,src,s) OR NOT decision_scope(w,src,s)
 THEN RETURN jsonb_build_object('code',404); END IF;
 SELECT * INTO mv FROM monitor_version WHERE (workspace_id,project_id,source_id,monitor_id,version_id)=(w,p,src,m,v);
 IF NOT FOUND OR k IS NULL THEN RETURN jsonb_build_object('code',404); END IF;
 PERFORM FROM browser_admission a JOIN import_batch i ON (i.workspace_id,i.source_id,i.file_sha256)=(w,src,a.checksum)
 JOIN snapshot x ON (x.workspace_id,x.source_id,x.batch_id)=(w,src,i.id) WHERE a.workspace_id=w AND a.source_id=src AND x.id=s AND a.monitor_allowed FOR SHARE OF a;
 IF NOT FOUND THEN RETURN jsonb_build_object('code',404); END IF;
 SELECT * INTO sn FROM snapshot WHERE (workspace_id,source_id,id)=(w,src,s);
 SELECT * INTO b FROM import_batch WHERE (workspace_id,source_id,id)=(w,src,sn.batch_id);
 IF sn.manifest_sha256<>snapshot_digest(w,src,b.id,sn.duckdb_sha256,sn.filters,sn.version_ids) OR b.accepted_count<>cardinality(sn.version_ids)
 OR b.input_count>10000 THEN RETURN jsonb_build_object('code',409,'reason','INVALID_RECEIPT'); END IF;
 IF parent_id IS NOT NULL THEN
   SELECT * INTO old FROM monitor_run x WHERE (x.workspace_id,x.project_id,x.source_id,x.monitor_id,x.version_id,x.id)=(w,p,src,m,v,parent_id);
   IF NOT FOUND OR NOT monitor_lock_grants(w,old.decision_id) THEN RETURN jsonb_build_object('code',404); END IF;
   SELECT * INTO pb FROM decision_binding WHERE (workspace_id,run_id)=(w,old.decision_id);
   SELECT ar.* INTO pa FROM analysis_run j JOIN artifact ar ON (ar.workspace_id,ar.run_id,ar.id)=(w,j.id,j.output_artifact_id)
   WHERE (j.workspace_id,j.id)=(w,old.decision_id) AND j.status='succeeded' AND ar.storage_state='available';
   IF NOT FOUND OR NOT decision_manifest_valid(w,old.decision_id) OR NOT EXISTS(SELECT FROM monitor_receipt WHERE (workspace_id,run_id)=(w,old.decision_id))
   OR NOT EXISTS(SELECT FROM snapshot os JOIN import_batch ob ON (ob.workspace_id,ob.source_id,ob.id)=(w,src,os.batch_id)
     WHERE (os.workspace_id,os.id)=(w,old.snapshot_id) AND (ob.registered_at,ob.id)<(b.registered_at,b.id))
   THEN RETURN jsonb_build_object('code',409,'reason','INCOMPATIBLE_PREDECESSOR'); END IF;
 END IF;
 SELECT x.* INTO replay FROM analysis_run j LEFT JOIN monitor_run x ON (x.workspace_id,x.decision_id)=(w,j.id)
 WHERE (j.workspace_id,j.request_key)=(w,k);
 IF FOUND THEN
   IF (replay.monitor_id,replay.version_id,replay.snapshot_id,replay.parent,replay.created_by) IS DISTINCT FROM (m,v,s,parent_id,access_actor())
   THEN RETURN jsonb_build_object('code',409); END IF;
   SELECT code=200 INTO ok FROM enqueue_decision(w,src,s,mv.aspect_id,mv.aspect_version_id,k,mv.payload->>'profile');
   IF ok IS NOT TRUE THEN RETURN jsonb_build_object('code',409); END IF;
   RETURN jsonb_build_object('code',200,'run_id',replay.id,'decision_run_id',replay.decision_id);
 END IF;
 IF EXISTS(SELECT FROM monitor_archive WHERE monitor_id=m) OR mv.version_no<>(SELECT max(version_no) FROM monitor_version WHERE monitor_id=m)
 OR EXISTS(SELECT FROM aspect WHERE aspect_id=mv.aspect_id AND archived_at IS NOT NULL) THEN RETURN jsonb_build_object('code',409); END IF;
 IF NOT (mv.payload->>'supported')::bool THEN RETURN jsonb_build_object('code',422,'reason','UNSUPPORTED_DEFINITION'); END IF;
 IF EXISTS(SELECT FROM monitor_run rr JOIN analysis_run j ON (j.workspace_id,j.id)=(w,rr.decision_id)
 WHERE rr.version_id=v AND (j.status IN ('queued','running') OR rr.snapshot_id=s AND j.status='succeeded'))
 THEN RETURN jsonb_build_object('code',409,'reason','ALREADY_REQUESTED'); END IF;
 SELECT * INTO job FROM enqueue_decision(w,src,s,mv.aspect_id,mv.aspect_version_id,k,mv.payload->>'profile');
 IF job.code<>202 THEN RETURN jsonb_build_object('code',job.code); END IF;
 INSERT INTO monitor_run(workspace_id,project_id,source_id,monitor_id,version_id,decision_id,snapshot_id,parent,
 snapshot_sha,manifest_sha,file_sha,parent_artifact,parent_rows,created_by)
 VALUES(w,p,src,m,v,job.run_id,s,parent_id,sn.duckdb_sha256,sn.manifest_sha256,b.file_sha256,pa.checksum,pb.manifest->>'rows_sha256',access_actor())
 RETURNING id INTO new_id;
 RETURN jsonb_build_object('code',202,'run_id',new_id,'decision_run_id',job.run_id);
END $$;

ALTER FUNCTION job_authorized(analysis_run) RENAME TO pre_monitor_job_authorized;
CREATE FUNCTION job_authorized(j analysis_run) RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT pre_monitor_job_authorized(j) AND monitor_job_allowed(j.workspace_id,j.id)
$$;
ALTER FUNCTION decision_authority(analysis_run) RENAME TO pre_monitor_decision_authority;
CREATE FUNCTION decision_authority(j analysis_run) RETURNS boolean LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT pre_monitor_decision_authority(j) THEN RETURN false; END IF;
 RETURN monitor_lock_grants(j.workspace_id,j.id);
END $$;

CREATE FUNCTION monitor_row_sha(x decision_row) RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT topic_hash(jsonb_build_array(x.version_id,x.content_sha256,x.text_sha256,x.outcome,x."start",x."end",x.counter_start,x.counter_end,x.score,x.reason))
$$;
CREATE FUNCTION monitor_parent(w uuid,r uuid,t uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run:=decision_fence(w,r,t); m monitor_run; old monitor_run; parent_doc jsonb; data jsonb;
BEGIN
 IF j.id IS NULL THEN RETURN NULL; END IF;
 SELECT * INTO m FROM monitor_run WHERE (workspace_id,decision_id)=(w,r);
 IF NOT FOUND THEN RETURN jsonb_build_object('monitor',false); END IF;
 IF m.parent IS NULL THEN RETURN jsonb_build_object('monitor',true,'parent',NULL); END IF;
 SELECT * INTO STRICT old FROM monitor_run WHERE id=m.parent;
 SELECT b.manifest INTO STRICT parent_doc FROM decision_binding b WHERE (b.workspace_id,b.run_id)=(w,old.decision_id);
 IF NOT decision_manifest_valid(w,old.decision_id) OR m.parent_rows IS DISTINCT FROM parent_doc->>'rows_sha256' THEN RETURN NULL; END IF;
 SELECT jsonb_build_object('monitor',true,'parent',old.id,'manifest',parent_doc,'artifact',a.checksum,'artifact_bytes',a.size_bytes,
 'snapshot',old.snapshot_sha,'file',i.file_sha256,'mapping',i.delivery->>'mapping_sha256','passport',i.delivery->>'passport_sha256',
 'coverage',i.delivery->>'coverage_sha256') INTO data FROM analysis_run aj JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=(w,aj.id,aj.output_artifact_id)
 JOIN snapshot sn ON (sn.workspace_id,sn.source_id,sn.id)=(w,old.source_id,old.snapshot_id)
 JOIN import_batch i ON (i.workspace_id,i.source_id,i.id)=(w,old.source_id,sn.batch_id)
 WHERE (aj.workspace_id,aj.id)=(w,old.decision_id) AND aj.status='succeeded' AND a.storage_state='available' AND a.checksum=m.parent_artifact;
 RETURN data;
END $$;

CREATE FUNCTION monitor_reuse_batch(w uuid,r uuid,t uuid,ids uuid[],runtime jsonb) RETURNS uuid[] LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run:=decision_fence(w,r,t); old monitor_run; m monitor_run; batch jsonb; hits uuid[];
BEGIN
 IF j.id IS NULL OR cardinality(ids) NOT BETWEEN 1 AND 32 OR runtime->>'dtype' IS DISTINCT FROM 'float64' OR NOT decision_runtime_valid(runtime)
 THEN RETURN NULL; END IF;
 SELECT * INTO STRICT m FROM monitor_run WHERE (workspace_id,decision_id)=(w,r);
 SELECT * INTO STRICT old FROM monitor_run WHERE id=m.parent;
 IF NOT EXISTS(SELECT FROM decision_binding b JOIN decision_binding n ON (n.workspace_id,n.run_id)=(w,r)
 WHERE (b.workspace_id,b.run_id)=(w,old.decision_id) AND b.manifest->'runtime'=runtime AND b.manifest->>'rows_sha256'=m.parent_rows
 AND (b.project_id,b.source_id,b.version_id,b.digest,b.profile,b.profile_sha256,b.threshold_set)=
 (n.project_id,n.source_id,n.version_id,n.digest,n.profile,n.profile_sha256,n.threshold_set)) THEN RETURN NULL; END IF;
 WITH candidates AS MATERIALIZED (
 SELECT x.* FROM unnest(ids) i(id)
 JOIN LATERAL (SELECT d.* FROM decision_row d WHERE (d.workspace_id,d.run_id,d.version_id)=(w,old.decision_id,i.id) LIMIT 1) x ON true
 JOIN LATERAL (SELECT version_id FROM snapshot_member WHERE (workspace_id,snapshot_id,version_id)=(w,j.snapshot_id,i.id) LIMIT 1) target ON true
 )
 SELECT coalesce(jsonb_agg(to_jsonb(c)-ARRAY['workspace_id','source_id','run_id','snapshot_id','generation']),'[]'),coalesce(array_agg(c.version_id),'{}'::uuid[])
 INTO batch,hits FROM candidates c;
 IF cardinality(hits)>0 THEN
   IF decision_stage(w,r,t,batch)<>cardinality(hits) THEN RETURN NULL; END IF;
   INSERT INTO monitor_reuse(workspace_id,run_id,version_id,parent_decision,parent_row_sha)
   SELECT w,r,d.version_id,old.decision_id,monitor_row_sha(d) FROM unnest(hits) h(id)
   JOIN LATERAL (SELECT x.* FROM decision_row x WHERE (x.workspace_id,x.run_id,x.version_id)=(w,old.decision_id,h.id) LIMIT 1) d ON true;
 END IF;
 RETURN hits;
END $$;

CREATE FUNCTION monitor_delta_rows(w uuid,src uuid,pre uuid,tgt uuid) RETURNS TABLE(identity_id uuid,previous_version_id uuid,target_version_id uuid,category text) LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
WITH selected AS MATERIALIZED (
    SELECT s.id FROM snapshot s
    WHERE s.workspace_id = w AND s.source_id = src
      AND s.id IN (pre, tgt)
      AND s.filters->>'selection' = 'accepted_in_delivery'
), observation AS MATERIALIZED (
    SELECT m.snapshot_id, v.identity_id, v.id AS version_id, i.external_review_id IS NOT NULL AS stable
    FROM snapshot_member m
    JOIN selected s ON s.id = m.snapshot_id
    JOIN review_version v ON (v.workspace_id, v.source_id, v.id) =
                             (m.workspace_id, m.source_id, m.version_id)
    JOIN review_identity i ON (i.workspace_id, i.source_id, i.id) =
                              (v.workspace_id, v.source_id, v.identity_id)
    WHERE m.workspace_id = w AND m.source_id = src
      AND m.snapshot_id IN (pre, tgt)
), previous AS (
    SELECT * FROM observation WHERE snapshot_id = pre
), target AS (
    SELECT * FROM observation WHERE snapshot_id = tgt
)
SELECT coalesce(t.identity_id, p.identity_id) AS identity_id,
       p.version_id AS previous_version_id, t.version_id AS target_version_id,
       CASE
         WHEN t.version_id IS NULL AND p.stable THEN 'not_observed_in_target'
         WHEN t.version_id IS NULL THEN 'unlinked_predecessor'
         WHEN pre IS NULL THEN 'initial_observed'
         WHEN NOT t.stable THEN 'unlinked_observation'
         WHEN p.version_id IS NULL THEN 'newly_observed'
         WHEN p.version_id = t.version_id THEN 'unchanged_version'
         ELSE 'changed_version'
       END AS category
FROM previous p FULL JOIN target t ON t.identity_id = p.identity_id
WHERE (SELECT count(*) FROM selected) = CASE WHEN pre IS NULL THEN 1 ELSE 2 END
ORDER BY identity_id
$$;

CREATE FUNCTION monitor_document(w uuid,r uuid) RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE m monitor_run; old monitor_run; b decision_binding; totals jsonb; delta_sha text; used int; used_eligible int; valid boolean;
 provenance_sha text; target_count int; previous_count int; summary jsonb; prev_runtime jsonb;
BEGIN
 SELECT * INTO STRICT m FROM monitor_run WHERE (workspace_id,decision_id)=(w,r);
 SELECT * INTO STRICT b FROM decision_binding WHERE (workspace_id,run_id)=(w,r);
 SELECT * INTO old FROM monitor_run WHERE id=m.parent;
 summary:=b.manifest->'summary';
 IF summary IS NULL OR b.manifest#>>'{runtime,dtype}' IS DISTINCT FROM 'float64' OR b.manifest->>'manifest_sha256'<>m.manifest_sha OR b.manifest->>'snapshot_sha256'<>m.snapshot_sha THEN RAISE EXCEPTION 'monitor_manifest_mismatch'; END IF;
 IF old.id IS NOT NULL THEN
   SELECT manifest->'runtime' INTO STRICT prev_runtime FROM decision_binding WHERE (workspace_id,run_id)=(w,old.decision_id);
 END IF;
 WITH changes AS MATERIALIZED (SELECT * FROM monitor_delta_rows(w,m.source_id,old.snapshot_id,m.snapshot_id))
 SELECT (SELECT jsonb_object_agg(category,n) FROM (SELECT category,count(*) n FROM changes GROUP BY category) g),
 encode(sha256(convert_to(coalesce(string_agg(topic_hash(to_jsonb(x)),'' ORDER BY identity_id),''),'UTF8')),'hex'),
 count(target_version_id),count(previous_version_id) INTO totals,delta_sha,target_count,previous_count FROM changes x;
 IF target_count<>(summary->>'N_accepted')::int OR old.id IS NOT NULL AND previous_count<>(SELECT cardinality(version_ids) FROM snapshot WHERE (workspace_id,id)=(w,old.snapshot_id)) THEN RAISE EXCEPTION 'monitor_delta_mismatch'; END IF;
 SELECT count(*),count(*) FILTER(WHERE x.outcome<>'excluded_blank'),bool_and(h.parent_decision=old.decision_id
 AND monitor_row_sha(x)=h.parent_row_sha AND monitor_row_sha(y)=h.parent_row_sha),
 encode(sha256(convert_to(coalesce(string_agg(topic_hash(jsonb_build_array(h.version_id,h.parent_decision,h.parent_row_sha)),'' ORDER BY h.version_id),''),'UTF8')),'hex')
 INTO used,used_eligible,valid,provenance_sha FROM monitor_reuse h JOIN decision_row x USING(workspace_id,run_id,version_id)
 JOIN decision_row y ON (y.workspace_id,y.run_id,y.version_id)=(w,h.parent_decision,h.version_id)
 WHERE (h.workspace_id,h.run_id)=(w,r);
 IF valid IS FALSE OR used>0 AND prev_runtime IS DISTINCT FROM b.manifest->'runtime'
 OR prev_runtime=b.manifest->'runtime' AND used<>coalesce((totals->>'unchanged_version')::int,0) THEN RAISE EXCEPTION 'monitor_provenance_mismatch'; END IF;
 RETURN jsonb_build_object('schema','monitor-receipt-1','run_id',m.id,'decision_run_id',r,'monitor_id',m.monitor_id,
 'monitor_version_id',m.version_id,'monitor_digest',(SELECT digest FROM monitor_version WHERE version_id=m.version_id),
 'snapshot_id',m.snapshot_id,'file_sha256',m.file_sha,'manifest_sha256',m.manifest_sha,'snapshot_sha256',m.snapshot_sha,
 'parent_run_id',old.id,'parent_decision_run_id',old.decision_id,'parent_snapshot_id',old.snapshot_id,'parent_manifest_sha256',old.manifest_sha,'parent_file_sha256',old.file_sha,
 'parent_artifact_sha256',m.parent_artifact,'parent_rows_sha256',m.parent_rows,'rows_sha256',b.manifest->>'rows_sha256',
 'runtime',b.manifest->'runtime','categories',coalesce(totals,'{}'),'delta_sha256',delta_sha,'provenance_sha256',provenance_sha,
 'change_state',CASE WHEN old.id IS NULL THEN 'INITIAL_DELIVERY' WHEN coalesce(totals,'{}')-'unchanged_version'='{}' THEN 'NO_VERSION_CHANGES' ELSE 'OBSERVED_CHANGES' END,
 'reuse_mode',CASE WHEN old.id IS NULL THEN 'FULL' WHEN prev_runtime=b.manifest->'runtime' THEN 'EXACT_VERSION' ELSE 'FULL_RUNTIME_CHANGED' END,
 'reused_eligible',used_eligible,'computed_eligible',(summary->>'N_eligible')::int-used_eligible,'reused_blank',used-used_eligible,
 'excluded_blank',(summary->>'N_excluded')::int,'accepted',target_count,'eligible',summary->'N_eligible','confirmed_source_removals',0,
 'removal_evidence','SOURCE_REMOVAL_EVIDENCE_UNAVAILABLE','event','NOT_EVALUATED','quality','QUALITY_GATE_NOT_MET');
END $$;
CREATE FUNCTION monitor_publish() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF EXISTS(SELECT FROM monitor_run WHERE (workspace_id,decision_id)=(NEW.workspace_id,NEW.id)) THEN
  IF session_user<>'rs_worker' OR NOT monitor_job_allowed(NEW.workspace_id,NEW.id) THEN RAISE EXCEPTION 'monitor_publish_denied'; END IF;
  INSERT INTO monitor_receipt VALUES(NEW.workspace_id,NEW.id,monitor_document(NEW.workspace_id,NEW.id),DEFAULT);
 END IF;
 RETURN NULL;
END $$;
CREATE TRIGGER monitor_publish AFTER UPDATE OF status ON analysis_run FOR EACH ROW WHEN
 (NEW.job_kind='snapshot_aspect_decision_v1' AND OLD.status='running' AND NEW.status='succeeded') EXECUTE FUNCTION monitor_publish();

-- The presence flag avoids any changes to the original PR-22 child or its normal projection.
ALTER FUNCTION job_input(uuid,uuid,uuid) RENAME TO pre_monitor_input;
REVOKE EXECUTE ON FUNCTION pre_monitor_input(uuid,uuid,uuid) FROM rs_worker;
CREATE FUNCTION job_input(w uuid,r uuid,t uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT pre_monitor_input(w,r,t) || CASE WHEN EXISTS(SELECT FROM monitor_run WHERE (workspace_id,decision_id)=(w,r))
 THEN '{"monitor":true}'::jsonb ELSE '{}'::jsonb END
$$;
REVOKE ALL ON FUNCTION job_input(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION job_input(uuid,uuid,uuid) TO rs_worker;

CREATE FUNCTION monitor_page(w uuid,p uuid,src uuid,m uuid,v uuid,section text,skip int,r uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE items jsonb; total int; mv monitor_version; mr monitor_run; pre monitor_run; choice uuid; latest uuid;
 last_ok timestamptz; archived boolean; doc jsonb; result jsonb; state text;
BEGIN
 IF skip IS NULL OR skip<0 OR NOT monitor_scope(w,p,src) THEN RETURN NULL; END IF;
 IF section='catalog' THEN
  WITH matches AS MATERIALIZED (
   SELECT x.monitor_id,x.created_at,head.payload->>'name' AS name,head.version_id,ar.monitor_id IS NOT NULL AS archived
   FROM monitor x JOIN LATERAL (SELECT payload,version_id FROM monitor_version WHERE monitor_id=x.monitor_id ORDER BY version_no DESC LIMIT 1) head ON true
   LEFT JOIN monitor_archive ar USING(monitor_id)
   WHERE (x.workspace_id,x.project_id,x.source_id)=(w,p,src)
  ) SELECT (SELECT count(*) FROM matches),coalesce(jsonb_agg(to_jsonb(pg) ORDER BY pg.created_at DESC,pg.monitor_id),'[]')
  INTO total,items FROM (SELECT * FROM matches ORDER BY created_at DESC,monitor_id OFFSET skip LIMIT 25) pg;
  RETURN jsonb_build_object('total',total,'offset',skip,'rows',items);
 END IF;
 SELECT * INTO mv FROM monitor_version WHERE (workspace_id,project_id,source_id,monitor_id)=(w,p,src,m) AND (v IS NULL OR version_id=v)
 ORDER BY version_no DESC LIMIT 1;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT EXISTS(SELECT FROM monitor_archive WHERE monitor_id=m) INTO archived;
 IF section='versions' THEN
  SELECT count(*) INTO total FROM monitor_version WHERE monitor_id=m;
  SELECT coalesce(jsonb_agg(to_jsonb(pg) ORDER BY pg.version_no DESC),'[]') INTO items FROM
  (SELECT version_id,version_no,created_by,created_at,digest,payload FROM monitor_version WHERE monitor_id=m ORDER BY version_no DESC OFFSET skip LIMIT 25) pg;
 ELSIF section IN ('snapshots','overview') THEN
  WITH matches AS MATERIALIZED (
   SELECT sn.id,b.id AS delivery_id,b.registered_at,b.received_at,b.coverage,b.input_count,b.accepted_count,sn.manifest_sha256,
          b.file_sha256 FROM snapshot sn JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id)
   WHERE (sn.workspace_id,sn.source_id)=(w,src) AND monitor_admitted(w,src,sn.id)
  ) SELECT (SELECT count(*) FROM matches),(SELECT id FROM matches ORDER BY registered_at DESC,delivery_id DESC LIMIT 1),
  coalesce(jsonb_agg(to_jsonb(pg) ORDER BY registered_at DESC,delivery_id DESC),'[]') INTO total,latest,items
  FROM (SELECT * FROM matches ORDER BY registered_at DESC,delivery_id DESC OFFSET skip LIMIT 25) pg;
  IF section='overview' THEN
    SELECT rr.snapshot_id,j.finished_at INTO choice,last_ok FROM monitor_run rr JOIN analysis_run j ON (j.workspace_id,j.id)=(w,rr.decision_id)
    JOIN snapshot x ON (x.workspace_id,x.id)=(w,rr.snapshot_id) JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,x.batch_id)
    WHERE rr.version_id=mv.version_id AND j.status='succeeded' AND monitor_job_allowed(w,rr.decision_id)
    ORDER BY b.registered_at DESC,b.id DESC,rr.created_at DESC LIMIT 1;
    state:=CASE WHEN archived THEN 'ARCHIVED' WHEN mv.version_no<>(SELECT max(version_no) FROM monitor_version WHERE monitor_id=m) THEN 'SUPERSEDED_VERSION'
     WHEN EXISTS(SELECT FROM aspect WHERE aspect_id=mv.aspect_id AND archived_at IS NOT NULL) THEN 'ASPECT_ARCHIVED' WHEN NOT (mv.payload->>'supported')::bool THEN 'UNSUPPORTED_DEFINITION'
     WHEN latest IS NULL THEN 'NO_SNAPSHOT' WHEN latest=choice THEN 'NO_NEW_DELIVERY' ELSE 'NEW_DELIVERY_AVAILABLE' END;
    RETURN jsonb_build_object('monitor_id',m,'version_id',mv.version_id,'version_no',mv.version_no,'payload',mv.payload,
     'digest',mv.digest,'created_by',mv.created_by,'created_at',mv.created_at,'archived',archived,'state',state,
     'latest_snapshot_id',latest,'last_success_at',last_ok,'schedule','MANUAL_ONLY');
  END IF;
 ELSIF section='history' THEN
  WITH matches AS MATERIALIZED (
   SELECT rr.id AS run_id,rr.version_id,rr.snapshot_id,rr.decision_id AS decision_run_id,rr.parent,rr.created_at,j.status,j.reason,j.finished_at
   FROM monitor_run rr JOIN analysis_run j ON (j.workspace_id,j.id)=(w,rr.decision_id)
   WHERE rr.monitor_id=m AND monitor_job_allowed(w,rr.decision_id) AND (v IS NULL OR rr.version_id=v)
  ) SELECT (SELECT count(*) FROM matches),coalesce(jsonb_agg(to_jsonb(pg) ORDER BY pg.created_at DESC,pg.run_id),'[]')
  INTO total,items FROM (SELECT * FROM matches ORDER BY created_at DESC,run_id OFFSET skip LIMIT 25) pg;
 ELSIF section IN ('run','delta') THEN
  SELECT * INTO mr FROM monitor_run WHERE (workspace_id,project_id,source_id,monitor_id,id)=(w,p,src,m,r) AND (v IS NULL OR version_id=v);
  IF NOT FOUND OR NOT monitor_lock_grants(w,mr.decision_id) THEN RETURN NULL; END IF;
  SELECT document INTO doc FROM monitor_receipt WHERE (workspace_id,run_id)=(w,mr.decision_id);
  IF section='run' THEN
   SELECT jsonb_build_object('run_id',mr.id,'monitor_version_id',mr.version_id,'decision_run_id',mr.decision_id,'snapshot_id',mr.snapshot_id,
    'source_id',src,'manifest_sha256',mr.manifest_sha,'file_sha256',mr.file_sha,'parent_run_id',mr.parent,'status',j.status,'stage',j.stage,'reason',j.reason,'processed',j.processed,'total',j.total,
    'registered_at',b.registered_at,'observed_at',sn.filters->'observed_at','coverage_passport',b.delivery->'coverage_passport','receipt',doc)
   INTO result FROM analysis_run j JOIN snapshot sn ON (sn.workspace_id,sn.id)=(w,mr.snapshot_id)
   JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id) WHERE (j.workspace_id,j.id)=(w,mr.decision_id);
   RETURN result;
  END IF;
  IF doc IS NULL THEN RETURN NULL; END IF;
  SELECT * INTO pre FROM monitor_run WHERE id=mr.parent;
  WITH changes AS MATERIALIZED (SELECT * FROM monitor_delta_rows(w,src,pre.snapshot_id,mr.snapshot_id))
  SELECT (SELECT count(*) FROM changes),coalesce(jsonb_agg(to_jsonb(pg) ORDER BY identity_id),'[]') INTO total,items FROM (
   SELECT c.*,h.parent_decision,h.parent_row_sha FROM (SELECT * FROM changes ORDER BY identity_id OFFSET skip LIMIT 25) c
   LEFT JOIN monitor_reuse h ON (h.workspace_id,h.run_id,h.version_id)=(w,mr.decision_id,c.target_version_id)) pg;
 ELSE RETURN NULL; END IF;
 RETURN jsonb_build_object('total',total,'offset',skip,'rows',items);
END $$;

CREATE OR REPLACE FUNCTION decision_visible(w uuid,r uuid) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT monitor_job_allowed(w,r) AND EXISTS(SELECT FROM decision_binding b WHERE (b.workspace_id,b.run_id)=(w,r)
 AND decision_scope(w,b.source_id,b.snapshot_id) AND aspect_scope(w,b.project_id))
$$;

CREATE OR REPLACE FUNCTION decision_exportable(w uuid,r uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT monitor_job_allowed(w,r) AND EXISTS(SELECT FROM decision_binding b WHERE (b.workspace_id,b.run_id)=(w,r) AND candidate_exportable(w,b.snapshot_id) AND decision_admitted(w,b.snapshot_id)
  AND aspect_project_allowed(w,b.project_id))
$$;

CREATE OR REPLACE FUNCTION decision_object(w uuid,src uuid,s uuid,r uuid,actor uuid,expires bigint,ticket text)
RETURNS TABLE(root text,digest text,bytes bigint,manifest text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT ws.delivery_object_root,a.checksum::text,a.size_bytes,sn.manifest_sha256::text
 FROM workspace ws JOIN snapshot sn ON (sn.workspace_id,sn.source_id,sn.id)=(w,src,s)
 JOIN analysis_run j ON (j.workspace_id,j.snapshot_id,j.id)=(w,s,r)
 JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=(w,r,j.output_artifact_id)
 JOIN decision_binding b ON (b.workspace_id,b.run_id,b.source_id,b.snapshot_id)=(w,r,src,s)
 JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(w,actor)
 JOIN principal p ON p.id=actor
 WHERE session_user='rs_upload' AND retention_online() AND ws.id=w AND ws.state='active'
 AND m.state='active' AND m.role IN ('owner','analyst') AND p.state='active'
 AND monitor_job_allowed(w,r) AND decision_admitted(w,s) AND aspect_project_allowed(w,b.project_id) AND b.manifest IS NOT NULL AND j.status='succeeded' AND j.job_kind='snapshot_aspect_decision_v1'
 AND (j.engine_version,j.definition_version)=('1','aspect-decision-1')
 AND a.storage_state='available' AND a.size_bytes BETWEEN 1 AND 65536
 AND expires BETWEEN floor(extract(epoch FROM statement_timestamp()))::bigint
 AND floor(extract(epoch FROM statement_timestamp()))::bigint+30
 AND EXISTS(SELECT FROM access_session x WHERE x.principal_id=actor AND x.revoked_at IS NULL
 AND x.expires_at>statement_timestamp() AND encode(sha256(x.token_hash || sha256(convert_to(
 concat_ws('|','aspect-decision-1',w,src,s,r,actor,expires),'UTF8'))),'hex')=ticket)
$$;

CREATE OR REPLACE FUNCTION decision_catalog(w uuid,src uuid,s uuid,a uuid,v uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('definition',d.def,'runs',coalesce((SELECT jsonb_agg(to_jsonb(t) ORDER BY created_at DESC,id) FROM (
    SELECT j.id,j.status,j.created_at FROM decision_binding b JOIN analysis_run j ON (j.workspace_id,j.id)=(w,b.run_id)
    WHERE (b.workspace_id,b.source_id,b.snapshot_id,b.aspect_id,b.version_id)=(w,src,s,a,v) AND monitor_job_allowed(w,b.run_id) ORDER BY j.created_at DESC,j.id LIMIT 25
 ) t),'[]'::jsonb)) FROM source z CROSS JOIN LATERAL (SELECT aspect_detail(w,z.project_id,a,v) def OFFSET 0) d
 WHERE (z.workspace_id,z.id)=(w,src) AND decision_scope(w,src,s) AND d.def IS NOT NULL
$$;

ALTER FUNCTION decision_read(uuid,uuid,uuid,uuid,text,int,uuid) RENAME TO pre_monitor_decision_read;
REVOKE EXECUTE ON FUNCTION pre_monitor_decision_read(uuid,uuid,uuid,uuid,text,int,uuid) FROM rs_runtime;
CREATE FUNCTION decision_read(w uuid,src uuid,s uuid,r uuid,f text,skip int,detail uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE data jsonb;
BEGIN
 data:=pre_monitor_decision_read(w,src,s,r,f,skip,detail);
 IF data IS NULL OR NOT monitor_lock_grants(w,r) THEN RETURN NULL; END IF;
 RETURN data;
END $$;
REVOKE ALL ON FUNCTION decision_read(uuid,uuid,uuid,uuid,text,int,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decision_read(uuid,uuid,uuid,uuid,text,int,uuid) TO rs_runtime;

-- Derived human/graph/topic publications must keep the admission of their machine parent.
ALTER FUNCTION human_scope(uuid,uuid,uuid,uuid,boolean,uuid) RENAME TO pre_monitor_human_scope;
CREATE FUNCTION human_scope(w uuid,src uuid,s uuid,r uuid,writing boolean,request_key uuid DEFAULT NULL) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT pre_monitor_human_scope(w,src,s,r,writing,request_key) THEN RETURN false; END IF;
 RETURN monitor_lock_grants(w,r);
END $$;
ALTER FUNCTION topic_scope(uuid,uuid,uuid,uuid,uuid) RENAME TO pre_monitor_topic_scope;
CREATE FUNCTION topic_scope(w uuid,src uuid,s uuid,r uuid,h uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT pre_monitor_topic_scope(w,src,s,r,h) THEN RETURN false; END IF;
 RETURN r IS NULL OR monitor_lock_grants(w,r);
END $$;
REVOKE ALL ON FUNCTION human_scope(uuid,uuid,uuid,uuid,boolean,uuid),topic_scope(uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;

CREATE FUNCTION monitor_export_sources() RETURNS TABLE(w uuid,src uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT e.* FROM retention_export_sources() e WHERE session_user='rs_retention'
$$;
CREATE FUNCTION monitor_export_runs() RETURNS TABLE(w uuid,r uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT m.workspace_id,m.decision_id FROM monitor_run m WHERE session_user='rs_retention' AND
 (m.workspace_id,m.source_id) IN (SELECT * FROM monitor_export_sources()) AND decision_exportable(m.workspace_id,m.decision_id)
$$;
ALTER FUNCTION retention_plan(uuid,text,uuid) RENAME TO pre_monitor_retention_plan;
CREATE FUNCTION retention_plan(w uuid,k text,t uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 WITH base AS (SELECT pre_monitor_retention_plan(w,k,t) d), sources AS MATERIALIZED (SELECT retention_sources(w,k,t) src),
 runs AS MATERIALIZED (SELECT decision_id FROM monitor_run WHERE workspace_id=w AND source_id IN (SELECT src FROM sources))
 SELECT jsonb_set(d,'{tables}',(d->'tables')||jsonb_build_object(
 'monitor',(SELECT count(*) FROM monitor WHERE workspace_id=w AND source_id IN (SELECT src FROM sources)),
 'monitor_version',(SELECT count(*) FROM monitor_version WHERE workspace_id=w AND source_id IN (SELECT src FROM sources)),
 'monitor_archive',(SELECT count(*) FROM monitor_archive WHERE workspace_id=w AND source_id IN (SELECT src FROM sources)),
 'monitor_run',(SELECT count(*) FROM runs),'monitor_receipt',(SELECT count(*) FROM monitor_receipt WHERE workspace_id=w AND run_id IN (SELECT decision_id FROM runs)),
 'monitor_reuse',(SELECT count(*) FROM monitor_reuse WHERE workspace_id=w AND run_id IN (SELECT decision_id FROM runs)))) FROM base
$$;
REVOKE ALL ON FUNCTION retention_plan(uuid,text,uuid) FROM PUBLIC;
CREATE FUNCTION monitor_intact(w uuid,r uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM monitor_run m JOIN analysis_run j ON (j.workspace_id,j.id)=(w,r)
 LEFT JOIN monitor_receipt z ON (z.workspace_id,z.run_id)=(w,r) JOIN monitor_version v ON v.version_id=m.version_id
 JOIN decision_binding b ON (b.workspace_id,b.run_id)=(w,r)
 WHERE (m.workspace_id,m.decision_id)=(w,r) AND (v.aspect_id,v.aspect_version_id,v.aspect_digest)=(b.aspect_id,b.version_id,b.digest)
 AND CASE WHEN j.status='succeeded' THEN z.document=monitor_document(w,r) ELSE z.document IS NULL END)
$$;

DO $$
DECLARE tab text; fn regprocedure; predicate text;
BEGIN
 FOREACH tab IN ARRAY ARRAY['monitor','monitor_version','monitor_archive','monitor_run','monitor_reuse','monitor_receipt'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tab);
  EXECUTE format('REVOKE ALL ON %I FROM PUBLIC,rs_runtime,rs_upload,rs_worker,rs_retention',tab);
  predicate:=CASE WHEN tab='monitor_run' THEN '(workspace_id,decision_id) IN (SELECT * FROM monitor_export_runs())'
   WHEN tab IN ('monitor_receipt','monitor_reuse') THEN '(workspace_id,run_id) IN (SELECT * FROM monitor_export_runs()) AND EXISTS(SELECT FROM decision_binding b WHERE b.workspace_id='||tab||'.workspace_id AND b.run_id='||tab||'.run_id AND b.manifest IS NOT NULL)'
   ELSE '(workspace_id,source_id) IN (SELECT * FROM monitor_export_sources())' END;
  EXECUTE format('CREATE POLICY owner_export ON %I FOR SELECT TO rs_retention USING (%s)',tab,predicate);
  EXECUTE format('GRANT SELECT ON %I TO rs_retention',tab);
  IF tab<>'monitor_reuse' THEN
   EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_history_change()',tab);
  END IF;
  EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION reject_history_change()',tab);
 END LOOP;
 FOR fn IN SELECT oid::regprocedure FROM pg_proc WHERE pronamespace='public'::regnamespace AND starts_with(proname,'monitor_') LOOP
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',fn);
 END LOOP;
END $$;
REVOKE ALL ON FUNCTION job_authorized(analysis_run),decision_authority(analysis_run) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION monitor_export_sources(),monitor_export_runs(),monitor_intact(uuid,uuid) TO rs_retention;
GRANT EXECUTE ON FUNCTION monitor_save(uuid,uuid,uuid,uuid,uuid,uuid,uuid,text,int,int,text,boolean),
 monitor_start(uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid),monitor_page(uuid,uuid,uuid,uuid,uuid,text,int,uuid) TO rs_runtime;
GRANT EXECUTE ON FUNCTION monitor_parent(uuid,uuid,uuid),monitor_reuse_batch(uuid,uuid,uuid,uuid[],jsonb) TO rs_worker;

CREATE FUNCTION monitor_object(w uuid,src uuid,s uuid,r uuid,actor uuid,expires bigint,ticket text)
RETURNS TABLE(root text,digest text,bytes bigint,manifest text,parents text[]) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT ws.delivery_object_root,sn.duckdb_sha256::text,67108864::bigint,sn.manifest_sha256::text,
 ARRAY[b.file_sha256::text,b.delivery->>'mapping_sha256',b.delivery->>'passport_sha256',b.delivery->>'coverage_sha256']
 FROM workspace ws JOIN snapshot sn ON (sn.workspace_id,sn.source_id,sn.id)=(w,src,s)
 JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id)
 JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(w,actor) JOIN principal p ON p.id=actor
 WHERE session_user='rs_upload' AND r=s AND ws.id=w AND ws.state='active' AND retention_online()
 AND m.state='active' AND m.role IN ('owner','analyst') AND p.state='active' AND monitor_admitted(w,src,s)
 AND expires BETWEEN floor(extract(epoch FROM statement_timestamp()))::bigint AND floor(extract(epoch FROM statement_timestamp()))::bigint+30
 AND EXISTS(SELECT FROM access_session x WHERE x.principal_id=actor AND x.revoked_at IS NULL AND x.expires_at>statement_timestamp()
 AND encode(sha256(x.token_hash || sha256(convert_to(concat_ws('|','monitor-snapshot-1',w,src,s,r,actor,expires),'UTF8'))),'hex')=ticket)
$$;
REVOKE ALL ON FUNCTION monitor_object(uuid,uuid,uuid,uuid,uuid,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION monitor_object(uuid,uuid,uuid,uuid,uuid,bigint,text) TO rs_upload;

CREATE FUNCTION monitor_roles_safe() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT (SELECT count(*)=6 AND bool_and(c.relrowsecurity) FROM pg_class c WHERE c.relnamespace='public'::regnamespace
 AND c.relname IN ('monitor','monitor_version','monitor_archive','monitor_run','monitor_reuse','monitor_receipt'))
 AND NOT EXISTS(SELECT FROM pg_roles u,pg_class c WHERE u.rolname IN ('rs_worker','rs_runtime','rs_upload')
 AND c.relnamespace='public'::regnamespace AND c.relkind='r' AND (c.relname='monitor' OR starts_with(c.relname,'monitor_'))
 AND (has_any_column_privilege(u.oid,c.oid,'SELECT,INSERT,UPDATE,REFERENCES') OR has_table_privilege(u.oid,c.oid,'DELETE,TRUNCATE,TRIGGER')))
 AND NOT EXISTS(SELECT FROM pg_roles u,pg_proc f WHERE u.rolname IN ('rs_worker','rs_runtime','rs_upload','rs_retention')
 AND f.pronamespace='public'::regnamespace AND (starts_with(f.proname,'monitor_') OR starts_with(f.proname,'pre_monitor_'))
 AND has_function_privilege(u.oid,f.oid,'EXECUTE') AND NOT (
 (u.rolname='rs_worker' AND f.proname IN ('monitor_parent','monitor_reuse_batch'))
 OR (u.rolname='rs_runtime' AND f.proname IN ('monitor_page','monitor_save','monitor_start'))
 OR (u.rolname='rs_upload' AND f.proname='monitor_object')
 OR (u.rolname='rs_retention' AND f.proname IN ('monitor_export_sources','monitor_export_runs','monitor_intact'))))
$$;
REVOKE ALL ON FUNCTION monitor_roles_safe() FROM PUBLIC;
ALTER FUNCTION browser_role_safe() RENAME TO pre26_browser_role_safe;
REVOKE EXECUTE ON FUNCTION pre26_browser_role_safe() FROM rs_upload;
CREATE FUNCTION browser_role_safe() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT pre26_browser_role_safe() AND monitor_roles_safe()
$$;
REVOKE ALL ON FUNCTION browser_role_safe() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_role_safe() TO rs_upload;
CREATE TRIGGER immutable_update BEFORE UPDATE ON monitor_reuse FOR EACH ROW EXECUTE FUNCTION reject_history_change();
CREATE FUNCTION monitor_guard_reuse_delete() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF pg_trigger_depth()>1 AND EXISTS(SELECT FROM analysis_run WHERE (workspace_id,id)=(OLD.workspace_id,OLD.run_id) AND status IN ('failed','cancelled','queued')) THEN RETURN OLD; END IF;
 IF session_user='rs_retention' AND EXISTS(SELECT FROM retention_request WHERE id=nullif(current_setting('rs.erasure',true),'')::uuid
 AND workspace_id=OLD.workspace_id AND action='erase' AND state='revoking') THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'immutable_monitor_reuse' USING ERRCODE='23514';
END $$;
REVOKE ALL ON FUNCTION monitor_guard_reuse_delete() FROM PUBLIC;
CREATE TRIGGER immutable_delete BEFORE DELETE ON monitor_reuse FOR EACH ROW EXECUTE FUNCTION monitor_guard_reuse_delete();
