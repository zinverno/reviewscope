-- No default lifetime. A rule is an independently approved instruction for one source/class.
CREATE TYPE retention_class AS ENUM ('raw','configuration','positions','versions','snapshot',
    'temporary','derivatives','report_export','diagnostics','backup');
CREATE TABLE retention_policy (
    workspace_id uuid NOT NULL, source_id uuid NOT NULL, class retention_class NOT NULL,
    anchor text NOT NULL CHECK (anchor IN ('received','registered','created')),
    days integer NOT NULL CHECK (days BETWEEN 1 AND 36500),
    review_by timestamptz NOT NULL, review_days integer NOT NULL CHECK(review_days BETWEEN 1 AND 3650), authority_ref uuid NOT NULL, approved_by uuid NOT NULL REFERENCES principal,
    hold_ref uuid, PRIMARY KEY (workspace_id,source_id,class),
    FOREIGN KEY (workspace_id,source_id) REFERENCES source
);
CREATE TABLE retention_control (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    installation uuid NOT NULL DEFAULT gen_random_uuid(), checkpoint text, recovery_binding text, enrolled boolean NOT NULL DEFAULT false
);
INSERT INTO retention_control DEFAULT VALUES;
CREATE TABLE retention_request (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), workspace_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('source','project','workspace')), target uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('export','erase')), request_key uuid NOT NULL,
    actor uuid, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL DEFAULT 'requested' CHECK (state IN
        ('requested','blocked','revoking','purging','verifying','completed','failed','revoked')),
    approved_at timestamptz, preview_sha sha256_hex, report jsonb, UNIQUE(workspace_id,request_key),
    CHECK (kind<>'workspace' OR target=workspace_id)
);
CREATE TABLE retention_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id uuid NOT NULL REFERENCES retention_request,
    state text NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION retention_transition() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF TG_OP='INSERT' OR NEW.state<>OLD.state THEN
        INSERT INTO retention_event(request_id,state) VALUES(NEW.id,NEW.state);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER retention_audit AFTER INSERT OR UPDATE OF state ON retention_request
FOR EACH ROW EXECUTE FUNCTION retention_transition();
-- The durable queue survives a database commit followed by a filesystem failure.
CREATE TABLE retention_object (
    request_id uuid NOT NULL REFERENCES retention_request, digest sha256_hex NOT NULL, classes text[] NOT NULL,
    outcome text CHECK (outcome IN ('erased','retained_with_reason')),
    PRIMARY KEY(request_id,digest)
);
CREATE VIEW retention_refs AS
    SELECT workspace_id,source_id,id AS parent,'raw' AS class,file_sha256::text AS digest FROM import_batch
    UNION ALL SELECT workspace_id,source_id,id,k,delivery->>k FROM import_batch
        CROSS JOIN unnest(ARRAY['mapping_sha256','passport_sha256','coverage_sha256']) k WHERE delivery IS NOT NULL
    UNION ALL SELECT workspace_id,source_id,id,'snapshot',duckdb_sha256 FROM snapshot
    UNION ALL SELECT a.workspace_id,s.source_id,a.id,'artifact',a.checksum FROM artifact a
        JOIN analysis_run j ON (j.workspace_id,j.id)=(a.workspace_id,a.run_id)
        JOIN snapshot s ON (s.workspace_id,s.id)=(j.workspace_id,j.snapshot_id);
CREATE FUNCTION retention_sources(w uuid,k text,t uuid) RETURNS SETOF uuid LANGUAGE sql STABLE AS $$
    SELECT id FROM source WHERE workspace_id=w AND (k='workspace' AND t=w OR
        k='project' AND project_id=t OR k='source' AND id=t)
$$;

-- Counts and per-parent references are safe for owner preview. Bind approval to the current graph.
CREATE FUNCTION retention_plan(w uuid,k text,t uuid) RETURNS jsonb LANGUAGE sql STABLE
SET TimeZone='UTC' SET search_path=pg_catalog,public,pg_temp AS $$
WITH sources AS MATERIALIZED (SELECT retention_sources(w,k,t) id),
 snaps AS MATERIALIZED (SELECT id FROM snapshot WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 runs AS MATERIALIZED (SELECT id FROM analysis_run WHERE workspace_id=w AND snapshot_id IN (SELECT id FROM snaps)),
 refs AS MATERIALIZED (SELECT r.* FROM retention_refs r WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 loc AS (SELECT l.id FROM location l WHERE workspace_id=w AND (k='workspace' OR k='project' AND project_id=t
      OR k='source' AND EXISTS (SELECT FROM source_location m WHERE m.workspace_id=w AND m.location_id=l.id AND m.project_id=l.project_id AND m.source_id=t)
        AND NOT EXISTS (SELECT FROM source_location m WHERE m.workspace_id=w AND m.location_id=l.id AND m.project_id=l.project_id AND m.source_id<>t)))
SELECT jsonb_build_object('contract','retention-1','scope',jsonb_build_array(w,k,t),
 'tables',jsonb_build_object(
 'workspace',(SELECT count(*) FROM workspace WHERE k='workspace' AND id=w),'client_project',(SELECT count(*) FROM client_project WHERE workspace_id=w AND (k='workspace' OR k='project' AND id=t)),
 'location',(SELECT count(*) FROM loc),'source',(SELECT count(*) FROM sources),
 'source_location',(SELECT count(*) FROM source_location WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 'delivery_authorization',(SELECT count(*) FROM delivery_authorization WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 'import_batch',(SELECT count(*) FROM import_batch WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 'import_row',(SELECT count(*) FROM import_row WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 'review_identity',(SELECT count(*) FROM review_identity WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 'review_version',(SELECT count(*) FROM review_version WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 'retention_policy',(SELECT count(*) FROM retention_policy WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 'snapshot',(SELECT count(*) FROM snaps),'snapshot_member',(SELECT count(*) FROM snapshot_member WHERE workspace_id=w AND snapshot_id IN (SELECT id FROM snaps)),
 'analysis_run',(SELECT count(*) FROM runs),'artifact',(SELECT count(*) FROM artifact WHERE workspace_id=w AND run_id IN (SELECT id FROM runs)),
 'workspace_membership',(SELECT count(*) FROM workspace_membership WHERE workspace_id=w AND k='workspace'),
 'membership_event',(SELECT count(*) FROM membership_event WHERE workspace_id=w AND k='workspace')),
 'policy_sha', (SELECT encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(p) ORDER BY p.source_id,p.class)::text,'[]'),'UTF8')),'hex')
    FROM retention_policy p WHERE workspace_id=w AND source_id IN (SELECT id FROM sources)),
 'references',coalesce((SELECT jsonb_agg(jsonb_build_array(source_id,parent,class,digest) ORDER BY source_id,parent,class,digest) FROM refs),'[]'),
 'shared',coalesce((SELECT jsonb_agg(digest ORDER BY digest) FROM (SELECT DISTINCT digest FROM retention_refs
   WHERE workspace_id=w AND digest IN (SELECT digest FROM refs) AND source_id NOT IN (SELECT id FROM sources)) x),'[]'),
 'backup','BACKUP_ERASURE_NOT_VERIFIED','downloads','external_uncontrolled','ml_index_cache','not_applicable')
$$;
CREATE FUNCTION affected_requests(w uuid,k text,t uuid,r uuid) RETURNS SETOF uuid LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT q.id FROM retention_request q WHERE workspace_id=w AND id IS DISTINCT FROM r
 AND (state IN ('requested','blocked') OR state='completed' AND action='export')
 AND (k='workspace' OR q.kind='workspace' OR (q.kind,q.target)=(k,t)
 OR k='source' AND q.kind='project' AND EXISTS(SELECT FROM source WHERE workspace_id=w AND id=t AND project_id=q.target)
 OR k='project' AND q.kind='source' AND EXISTS(SELECT FROM source WHERE workspace_id=w AND project_id=t AND id=q.target))
$$;
CREATE FUNCTION erasure_plan(w uuid,k text,t uuid,r uuid) RETURNS jsonb LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT retention_plan(w,k,t)||jsonb_build_object('revoked_requests',ARRAY(SELECT id FROM affected_requests(w,k,t,r) id ORDER BY id))
$$;
REVOKE ALL ON FUNCTION affected_requests(uuid,text,uuid,uuid),erasure_plan(uuid,text,uuid,uuid) FROM PUBLIC;
CREATE FUNCTION retention_owner(w uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT coalesce(w=nullif(current_setting('rs.workspace',true),'')::uuid,false) AND EXISTS
   (SELECT FROM workspace_membership m JOIN workspace ws ON ws.id=w WHERE m.workspace_id=w
    AND ws.state='active' AND principal_id=access_actor() AND m.role='owner' AND m.state='active')
$$;
CREATE FUNCTION request_retention(w uuid,k text,t uuid,a text,key uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE req retention_request; plan jsonb;
BEGIN
 IF NOT retention_owner(w) THEN RETURN NULL; END IF;
 PERFORM FROM workspace WHERE id=w FOR UPDATE;
 IF NOT retention_owner(w) THEN RETURN NULL; END IF;
 SELECT * INTO req FROM retention_request WHERE workspace_id=w AND request_key=key;
 IF FOUND THEN
    IF (req.kind,req.target,req.action) IS DISTINCT FROM (k,t,a) THEN RETURN '{"conflict":true}'; END IF;
 ELSE
    IF NOT (k='workspace' AND w=t OR k='project' AND EXISTS (SELECT FROM client_project WHERE workspace_id=w AND id=t)
        OR k='source' AND EXISTS (SELECT FROM source WHERE workspace_id=w AND id=t)) THEN RETURN NULL; END IF;
    plan:=CASE WHEN a='erase' THEN erasure_plan(w,k,t,NULL) ELSE retention_plan(w,k,t) END;
    IF EXISTS(SELECT FROM jsonb_array_elements(plan->'references') v WHERE v->>3 IS NULL OR v->>3 !~ '^[0-9a-f]{64}$') THEN
        RAISE EXCEPTION 'invalid_object_reference'; END IF;
    IF octet_length(plan::text)>2097152 THEN RAISE EXCEPTION 'preview_bound'; END IF;
    INSERT INTO retention_request(workspace_id,kind,target,action,request_key,actor,preview_sha,report)
        VALUES(w,k,t,a,key,access_actor(),encode(sha256(convert_to(plan::text,'UTF8')),'hex'),plan) RETURNING * INTO req;
 END IF;
 RETURN jsonb_build_object('id',req.id,'state',req.state,'preview_sha',req.preview_sha,'report',req.report);
END $$;
CREATE FUNCTION retention_status(w uuid,r uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('id',id,'state',state,'action',action,'preview_sha',preview_sha,'report',report)
 FROM retention_request WHERE workspace_id=w AND id=r AND retention_owner(w)
$$;
-- Receipt clocks, canonical version creation and worker publication are distinct events.
CREATE VIEW retention_clocks AS
 SELECT b.workspace_id,b.source_id,c::retention_class AS class,b.id AS object_id,b.id AS batch_id,
    b.received_at,b.registered_at,NULL::timestamptz AS created_at
 FROM import_batch b CROSS JOIN unnest(ARRAY['raw','configuration','positions']) c WHERE c='raw' OR b.delivery IS NOT NULL
 UNION ALL SELECT v.workspace_id,v.source_id,'versions',v.id,b.id,b.received_at,b.registered_at,v.created_at
    FROM review_version v JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(v.workspace_id,v.source_id,v.batch_id)
 UNION ALL SELECT s.workspace_id,s.source_id,'snapshot',s.id,b.id,b.received_at,b.registered_at,s.created_at
    FROM snapshot s JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(s.workspace_id,s.source_id,s.batch_id)
 UNION ALL SELECT s.workspace_id,s.source_id,CASE WHEN a.kind IN ('report','export') THEN 'report_export' ELSE 'derivatives' END::retention_class,
    a.id,b.id,b.received_at,b.registered_at,a.created_at FROM artifact a JOIN analysis_run j ON (j.workspace_id,j.id)=(a.workspace_id,a.run_id)
    JOIN snapshot s ON (s.workspace_id,s.id)=(j.workspace_id,j.snapshot_id)
    JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(s.workspace_id,s.source_id,s.batch_id);
CREATE FUNCTION retention_terms(w uuid,s uuid) RETURNS SETOF jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET TimeZone='UTC' SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('class',c,'object_id',i.object_id,'delivery_id',i.batch_id,'policy',CASE WHEN p.days IS NULL THEN 'unset' ELSE 'approved' END,
  'days',p.days,'anchor',p.anchor,'review_by',p.review_by,'review_days',p.review_days,'hold',p.hold_ref IS NOT NULL,
  'expires_at',(CASE p.anchor WHEN 'received' THEN i.received_at WHEN 'registered' THEN i.registered_at WHEN 'created' THEN i.created_at END)+make_interval(days=>p.days),
  'coverage',coalesce(b.coverage,'unknown'),'backup','BACKUP_ERASURE_NOT_VERIFIED',
  'erasure',(SELECT state FROM retention_request WHERE workspace_id=w AND action='erase' AND
      (kind='workspace' OR kind='source' AND target=s OR kind='project' AND target=src.project_id) ORDER BY created_at DESC LIMIT 1))
 FROM source src CROSS JOIN unnest(enum_range(NULL::retention_class)) c
 LEFT JOIN retention_policy p ON (p.workspace_id,p.source_id,p.class)=(w,s,c)
 LEFT JOIN retention_clocks i ON (i.workspace_id,i.source_id,i.class)=(w,s,c)
 LEFT JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,s,i.batch_id)
 WHERE (src.workspace_id,src.id)=(w,s) AND w=nullif(current_setting('rs.workspace',true),'')::uuid
 AND EXISTS (SELECT FROM workspace_membership WHERE workspace_id=w AND principal_id=access_actor() AND state='active')
 ORDER BY c,i.object_id
$$;
-- A deleted UUID cannot be reintroduced by a later operator import/provision.
CREATE FUNCTION prevent_resurrection() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE rec jsonb:=to_jsonb(NEW); w uuid:=coalesce((rec->>'workspace_id')::uuid,NEW.id);
BEGIN
 IF EXISTS (SELECT FROM retention_request WHERE workspace_id=w AND action='erase' AND approved_at IS NOT NULL
     AND (kind='workspace' OR (kind='project' AND target=CASE WHEN TG_TABLE_NAME='client_project' THEN NEW.id ELSE (rec->>'project_id')::uuid END)
         OR (kind='source' AND TG_TABLE_NAME='source' AND target=NEW.id))) THEN
    RAISE EXCEPTION 'scope_revoked' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER retained_workspace BEFORE INSERT ON workspace FOR EACH ROW EXECUTE FUNCTION prevent_resurrection();
CREATE TRIGGER retained_project BEFORE INSERT ON client_project FOR EACH ROW EXECUTE FUNCTION prevent_resurrection();
CREATE TRIGGER retained_source BEFORE INSERT ON source FOR EACH ROW EXECUTE FUNCTION prevent_resurrection();

-- Both ends of the run/artifact cycle are removed within one checked transaction.
ALTER TABLE analysis_run ALTER CONSTRAINT analysis_run_workspace_id_id_output_artifact_id_fkey DEFERRABLE INITIALLY IMMEDIATE;
CREATE OR REPLACE FUNCTION reject_history_change() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP='DELETE' AND session_user='rs_retention' AND EXISTS (
   SELECT FROM retention_request WHERE id=nullif(current_setting('rs.erasure',true),'')::uuid AND state='revoking'
   AND workspace_id=OLD.workspace_id AND action='erase') THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'immutable operational history' USING ERRCODE='23514';
END $$;

CREATE FUNCTION revoke_retention(r uuid,w uuid,k text,t uuid,key uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE sources uuid[]; snaps uuid[]; runs uuid[]; locs uuid[]; projects uuid[]; tab text;
BEGIN
 IF session_user<>'rs_retention' OR k NOT IN ('workspace','project','source') OR (k='workspace' AND t<>w)
    THEN RAISE EXCEPTION 'scope_refused'; END IF;
 -- Recovery may replay an authorization issued after the restored backup point.
 IF NOT EXISTS (SELECT FROM retention_request WHERE (id,workspace_id,kind,target,request_key,action)=(r,w,k,t,key,'erase') AND approved_at IS NOT NULL)
    THEN RAISE EXCEPTION 'scope_conflict'; END IF;
 SELECT ARRAY(SELECT retention_sources(w,k,t)) INTO sources;
 SELECT ARRAY(SELECT id FROM snapshot WHERE workspace_id=w AND source_id=ANY(sources)) INTO snaps;
 SELECT ARRAY(SELECT id FROM analysis_run WHERE workspace_id=w AND snapshot_id=ANY(snaps)) INTO runs;
 SELECT ARRAY(SELECT id FROM client_project WHERE workspace_id=w AND (k='workspace' OR k='project' AND id=t)) INTO projects;
 SELECT ARRAY(SELECT DISTINCT m.location_id FROM source_location m WHERE m.workspace_id=w AND m.source_id=ANY(sources)
    AND NOT EXISTS (SELECT FROM source_location keep WHERE keep.workspace_id=w AND keep.project_id=m.project_id
        AND keep.location_id=m.location_id AND NOT keep.source_id=ANY(sources))) INTO locs;
 IF k='source' THEN SELECT ARRAY(SELECT project_id FROM source WHERE workspace_id=w AND id=t) INTO projects; END IF;
 UPDATE retention_request SET report=NULL,state='revoked' WHERE id IN (SELECT affected_requests(w,k,t,r));
 UPDATE retention_request SET report=jsonb_build_object('tables',retention_plan(w,k,t)->'tables'),state='revoking' WHERE id=r;
 INSERT INTO retention_object(request_id,digest,classes)
 SELECT r,digest,array_agg(DISTINCT CASE WHEN class IN ('mapping_sha256','passport_sha256','coverage_sha256')
    THEN 'configuration' WHEN class='artifact' THEN 'worker_artifacts' ELSE class END)
 FROM retention_refs WHERE workspace_id=w AND source_id=ANY(sources) GROUP BY digest ON CONFLICT DO NOTHING;
 PERFORM set_config('rs.erasure',r::text,true);
 SET CONSTRAINTS ALL DEFERRED;
 DELETE FROM artifact WHERE workspace_id=w AND run_id=ANY(runs);
 DELETE FROM analysis_run WHERE workspace_id=w AND id=ANY(runs);
 DELETE FROM snapshot_member WHERE workspace_id=w AND source_id=ANY(sources);
 DELETE FROM snapshot WHERE workspace_id=w AND source_id=ANY(sources);
 FOREACH tab IN ARRAY ARRAY['import_row','review_version','review_identity','import_batch','retention_policy','delivery_authorization','source_location'] LOOP
    EXECUTE format('DELETE FROM %I WHERE workspace_id=$1 AND source_id=ANY($2)',tab) USING w,sources;
 END LOOP;
 DELETE FROM source WHERE workspace_id=w AND id=ANY(sources);
 DELETE FROM location WHERE workspace_id=w AND project_id=ANY(projects) AND (k<>'source' OR id=ANY(locs));
 DELETE FROM client_project WHERE k<>'source' AND workspace_id=w AND id=ANY(projects);
 IF k='workspace' THEN
    DELETE FROM workspace_membership WHERE workspace_id=w;
    DELETE FROM membership_event WHERE workspace_id=w;
    DELETE FROM workspace WHERE id=w;
 END IF;
 SET CONSTRAINTS ALL IMMEDIATE;
 UPDATE retention_request SET state='purging' WHERE id=r;
END $$;

CREATE FUNCTION retention_eligible(r uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM retention_request q JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(q.workspace_id,q.actor)
   JOIN principal p ON p.id=m.principal_id JOIN workspace w ON w.id=m.workspace_id
   WHERE q.id=r AND q.state IN ('requested','blocked') AND m.role='owner' AND m.state='active' AND p.state='active' AND w.state='active')
$$;
CREATE FUNCTION retention_export_sources() RETURNS TABLE(w uuid,s uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT q.workspace_id,src.id FROM retention_request q CROSS JOIN LATERAL retention_sources(q.workspace_id,q.kind,q.target) src(id)
 WHERE q.id=nullif(current_setting('rs.export_request',true),'')::uuid AND q.action='export' AND retention_eligible(q.id)
$$;
DO $$
DECLARE tab text;
BEGIN
 FOREACH tab IN ARRAY ARRAY['import_batch','import_row','snapshot','review_identity','review_version','snapshot_member','source_location','source'] LOOP
   EXECUTE format('CREATE POLICY owner_export ON %I FOR SELECT TO rs_retention USING ((workspace_id,%I) IN (SELECT * FROM retention_export_sources()))',
      tab,CASE WHEN tab='source' THEN 'id' ELSE 'source_id' END);
   EXECUTE format('GRANT SELECT ON %I TO rs_retention',tab);
 END LOOP;
END $$;
CREATE POLICY owner_export ON analysis_run FOR SELECT TO rs_retention USING (EXISTS
    (SELECT FROM snapshot WHERE workspace_id=analysis_run.workspace_id AND id=analysis_run.snapshot_id));
CREATE POLICY owner_export ON artifact FOR SELECT TO rs_retention USING (storage_state='available' AND EXISTS
    (SELECT FROM analysis_run WHERE workspace_id=artifact.workspace_id AND id=artifact.run_id));
GRANT SELECT(workspace_id,id,snapshot_id,engine_version,model_version,definition_version,created_at,finished_at,status,
    output_artifact_id,job_kind,attempt,stage,processed,total,reason) ON analysis_run TO rs_retention;
GRANT SELECT ON artifact TO rs_retention;
CREATE FUNCTION retention_root(w uuid) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$ SELECT delivery_object_root FROM workspace WHERE id=w $$;
CREATE FUNCTION retention_held(w uuid,k text,t uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM retention_policy WHERE workspace_id=w AND hold_ref IS NOT NULL AND source_id IN (SELECT retention_sources(w,k,t)))
$$;
CREATE FUNCTION retention_preview(w uuid,k text,t uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$ SELECT retention_plan(w,k,t) $$;
CREATE FUNCTION retention_referenced(w uuid,d text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$ SELECT EXISTS(SELECT FROM retention_refs WHERE workspace_id=w AND digest=d) $$;
CREATE FUNCTION retention_stamp() RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('installation',installation,'checkpoint',checkpoint,'database',current_database(),
  'oid',(SELECT oid::text FROM pg_database WHERE datname=current_database()),'boot',extract(epoch FROM pg_postmaster_start_time())::text)
 FROM retention_control
$$;

ALTER TABLE retention_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE retention_object ENABLE ROW LEVEL SECURITY;
ALTER TABLE retention_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE retention_policy ENABLE ROW LEVEL SECURITY;
CREATE POLICY operator_requests ON retention_request TO rs_retention USING(true);
CREATE POLICY operator_objects ON retention_object TO rs_retention USING(true);
GRANT USAGE ON SCHEMA public TO rs_retention;
GRANT SELECT ON retention_request,retention_object TO rs_retention;
GRANT UPDATE(state,report) ON retention_request TO rs_retention;
GRANT UPDATE(outcome) ON retention_object TO rs_retention;
GRANT SELECT,UPDATE(checkpoint) ON retention_control TO rs_retention;
ALTER FUNCTION retention_transition() SECURITY DEFINER;
REVOKE ALL ON FUNCTION retention_transition(),retention_sources(uuid,text,uuid),retention_plan(uuid,text,uuid),
 retention_owner(uuid),request_retention(uuid,text,uuid,text,uuid),retention_status(uuid,uuid),retention_terms(uuid,uuid),
 prevent_resurrection(),revoke_retention(uuid,uuid,text,uuid,uuid),retention_eligible(uuid),retention_export_sources(),
 retention_root(uuid),retention_held(uuid,text,uuid),retention_preview(uuid,text,uuid),retention_referenced(uuid,text),retention_stamp() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION retention_stamp() TO rs_worker,rs_runtime,rs_retention;
GRANT EXECUTE ON FUNCTION request_retention(uuid,text,uuid,text,uuid),retention_status(uuid,uuid),retention_terms(uuid,uuid) TO rs_runtime;
GRANT EXECUTE ON FUNCTION revoke_retention(uuid,uuid,text,uuid,uuid),retention_eligible(uuid),retention_export_sources(),
 retention_root(uuid),retention_held(uuid,text,uuid),retention_preview(uuid,text,uuid),retention_referenced(uuid,text) TO rs_retention;
ALTER TABLE retention_request ADD COLUMN device bigint, ADD COLUMN inode bigint;
ALTER TABLE retention_object ADD COLUMN verified boolean NOT NULL DEFAULT false;
GRANT UPDATE(device,inode) ON retention_request TO rs_retention;
GRANT UPDATE(verified) ON retention_object TO rs_retention;

-- Export project/location metadata only where the request's selection reaches that row.
CREATE FUNCTION export_project(w uuid,p uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (SELECT FROM retention_request q WHERE q.id=nullif(current_setting('rs.export_request',true),'')::uuid
 AND q.action='export' AND q.workspace_id=w AND retention_eligible(q.id) AND (kind='workspace' OR kind='project' AND target=p
 OR kind='source' AND EXISTS(SELECT FROM source WHERE workspace_id=w AND project_id=p AND id=q.target)))
$$;
CREATE POLICY owner_export ON client_project FOR SELECT TO rs_retention USING (export_project(workspace_id,id));
CREATE POLICY owner_export ON location FOR SELECT TO rs_retention USING (export_project(workspace_id,project_id) AND
 (EXISTS(SELECT FROM source_location m WHERE (m.workspace_id,m.project_id,m.location_id)=(location.workspace_id,location.project_id,location.id))
 OR EXISTS(SELECT FROM retention_request q WHERE q.id=nullif(current_setting('rs.export_request',true),'')::uuid AND q.kind<>'source')));
GRANT SELECT ON client_project,location TO rs_retention;
REVOKE ALL ON FUNCTION export_project(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION export_project(uuid,uuid) TO rs_retention;
CREATE FUNCTION retention_current_owner(r uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM retention_request q JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(q.workspace_id,q.actor)
  JOIN principal p ON p.id=m.principal_id JOIN workspace ws ON ws.id=m.workspace_id WHERE q.id=r AND ws.state='active'
  AND m.role='owner' AND m.state='active' AND p.state='active')
$$;
REVOKE ALL ON FUNCTION retention_current_owner(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION retention_current_owner(uuid) TO rs_retention;
CREATE FUNCTION retention_access_seal() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET TimeZone='UTC' SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT encode(sha256(convert_to(coalesce(string_agg(fingerprint,'' ORDER BY fingerprint),''),'UTF8')),'hex')
 FROM (SELECT encode(sha256(convert_to(row_to_json(v)::text,'UTF8')),'hex') AS fingerprint FROM (
   SELECT 'principal' AS t,jsonb_build_array(id,login,state) AS x FROM principal
   UNION ALL SELECT 'credential',jsonb_build_array(principal_id,encode(salt,'hex'),encode(verifier,'hex')) FROM password_credential
   UNION ALL SELECT 'membership',to_jsonb(m) FROM workspace_membership m
   UNION ALL SELECT 'grant',to_jsonb(a) FROM delivery_authorization a
   UNION ALL SELECT 'workspace',jsonb_build_array(id,state) FROM workspace
   UNION ALL SELECT 'retention_policy',to_jsonb(p) FROM retention_policy p
 ) v) hashes
$$;
REVOKE ALL ON FUNCTION retention_access_seal() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION retention_access_seal() TO rs_retention;
CREATE FUNCTION approve_retention(r uuid,expected sha256_hex) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE q retention_request;
BEGIN
 SELECT * INTO STRICT q FROM retention_request WHERE id=r AND action='erase' FOR UPDATE;
 IF NOT retention_eligible(r) OR q.preview_sha IS DISTINCT FROM expected OR retention_held(q.workspace_id,q.kind,q.target)
    OR expected IS DISTINCT FROM encode(sha256(convert_to(erasure_plan(q.workspace_id,q.kind,q.target,r)::text,'UTF8')),'hex') THEN
    RETURN false; END IF;
 UPDATE retention_request SET approved_at=clock_timestamp() WHERE id=r;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION approve_retention(uuid,sha256_hex) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION approve_retention(uuid,sha256_hex) TO rs_retention;
CREATE FUNCTION approve_policy(w uuid,s uuid,c retention_class,basis uuid,clock_name text,lifetime int,review int,hold uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT retention_owner(w) THEN RETURN false; END IF;
 PERFORM FROM workspace WHERE id=w FOR UPDATE;
 IF NOT retention_owner(w) OR NOT EXISTS(SELECT FROM source WHERE workspace_id=w AND id=s) THEN RETURN false; END IF;
 INSERT INTO retention_policy(workspace_id,source_id,class,authority_ref,approved_by,anchor,days,review_by,review_days,hold_ref)
 VALUES(w,s,c,basis,access_actor(),clock_name,lifetime,clock_timestamp()+make_interval(days=>review),review,hold)
 ON CONFLICT(workspace_id,source_id,class) DO UPDATE SET authority_ref=excluded.authority_ref,approved_by=excluded.approved_by,
 anchor=excluded.anchor,days=excluded.days,review_by=excluded.review_by,review_days=excluded.review_days,hold_ref=excluded.hold_ref;
 RETURN true;
END $$;
CREATE FUNCTION retention_sweep(w uuid,s uuid,key uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET TimeZone='UTC' SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE due bigint; blocked bigint;
BEGIN
 IF NOT retention_owner(w) THEN RETURN NULL; END IF;
 PERFORM FROM workspace WHERE id=w FOR UPDATE;
 IF NOT retention_owner(w) OR NOT EXISTS(SELECT FROM source WHERE workspace_id=w AND id=s) THEN RETURN NULL; END IF;
 SELECT count(*) FILTER(WHERE eligible),count(*) FILTER(WHERE eligible IS NOT TRUE) INTO due,blocked FROM
 (SELECT NOT (p.review_by<=statement_timestamp() OR p.hold_ref IS NOT NULL) AND b.coverage IN ('partial','full')
   AND (CASE p.anchor WHEN 'received' THEN i.received_at WHEN 'registered' THEN i.registered_at WHEN 'created' THEN i.created_at END)
     +make_interval(days=>p.days)<=statement_timestamp() AS eligible
  FROM retention_clocks i LEFT JOIN retention_policy p USING(workspace_id,source_id,class)
  JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(i.workspace_id,i.source_id,i.batch_id)
  WHERE i.workspace_id=w AND i.source_id=s) evidence;
 IF due=0 OR blocked>0 OR retention_held(w,'source',s) THEN
  RETURN jsonb_build_object('state','BLOCKED_RETENTION_POLICY','due',due,'blocked',blocked);
 END IF;
 RETURN request_retention(w,'source',s,'erase',key);
END $$;
REVOKE ALL ON FUNCTION approve_policy(uuid,uuid,retention_class,uuid,text,int,int,uuid),retention_sweep(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION approve_policy(uuid,uuid,retention_class,uuid,text,int,int,uuid),retention_sweep(uuid,uuid,uuid) TO rs_runtime;
ALTER TABLE retention_control ADD COLUMN live_binding jsonb;
GRANT UPDATE(live_binding) ON retention_control TO rs_retention;
CREATE FUNCTION retention_online() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT checkpoint IS NOT NULL AND live_binding=retention_stamp()-'checkpoint' FROM retention_control
$$;
CREATE OR REPLACE FUNCTION access_actor() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT s.principal_id FROM access_session s JOIN principal p ON p.id=s.principal_id
 WHERE retention_online() AND s.token_hash=sha256(convert_to(nullif(current_setting('rs.session_proof',true),''),'UTF8'))
 AND s.revoked_at IS NULL AND s.expires_at>statement_timestamp() AND p.state='active'
$$;
ALTER FUNCTION claim_job() RENAME TO claim_ready_job;
CREATE FUNCTION claim_job() RETURNS TABLE(w uuid,r uuid,token uuid,generation integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF retention_online() THEN RETURN QUERY SELECT * FROM claim_ready_job(); END IF;
END $$;
ALTER FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) RENAME TO step_ready_job;
CREATE FUNCTION worker_step(w uuid,r uuid,t uuid,next_stage text,n integer,den integer,
 problem text DEFAULT NULL,digest sha256_hex DEFAULT NULL,bytes integer DEFAULT NULL,manifest sha256_hex DEFAULT NULL)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT coalesce(retention_online(),false) THEN RETURN 'stale'; END IF;
 RETURN step_ready_job(w,r,t,next_stage,n,den,problem,digest,bytes,manifest);
END $$;
CREATE OR REPLACE FUNCTION live_job(w uuid,r uuid,t uuid) RETURNS SETOF analysis_run LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT j.* FROM analysis_run j WHERE retention_online() AND j.workspace_id=w AND j.id=r AND j.lease_token=t
  AND j.status='running' AND j.lease_until>statement_timestamp() AND NOT j.cancel_requested AND job_authorized(j)
$$;
REVOKE ALL ON FUNCTION claim_ready_job(),step_ready_job(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM rs_worker;
REVOKE ALL ON FUNCTION retention_online(),claim_job(),worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_job(),worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) TO rs_worker;
CREATE POLICY owner_export ON workspace FOR SELECT TO rs_retention USING (EXISTS
 (SELECT FROM retention_request q WHERE q.id=nullif(current_setting('rs.export_request',true),'')::uuid
  AND q.workspace_id=workspace.id AND action='export' AND retention_eligible(q.id)));
GRANT SELECT(id,name,state,created_at) ON workspace TO rs_retention;
CREATE POLICY owner_export ON retention_policy FOR SELECT TO rs_retention USING
 ((workspace_id,source_id) IN (SELECT * FROM retention_export_sources()));
GRANT SELECT ON retention_policy TO rs_retention;
-- Drop per-file fingerprints once reconciliation is complete. Carry durable unlink evidence
-- across overlapping requests before releasing the completed queue.
CREATE FUNCTION finish_retention(r uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE q retention_request;
BEGIN
 SELECT * INTO STRICT q FROM retention_request WHERE id=r AND action='erase' AND approved_at IS NOT NULL AND state='completed';
 IF EXISTS(SELECT FROM retention_object WHERE request_id=r AND (NOT verified OR outcome IS NULL)) THEN
    RAISE EXCEPTION 'verification_incomplete'; END IF;
 UPDATE retention_object o SET verified=true FROM retention_request pending
 WHERE pending.id=o.request_id AND pending.id<>r AND (pending.workspace_id,pending.device,pending.inode)=(q.workspace_id,q.device,q.inode)
 AND EXISTS(SELECT FROM retention_object done WHERE done.request_id=r AND done.digest=o.digest AND done.outcome='erased');
 DELETE FROM retention_object WHERE request_id=r;
END $$;
REVOKE ALL ON FUNCTION finish_retention(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION finish_retention(uuid) TO rs_retention;
