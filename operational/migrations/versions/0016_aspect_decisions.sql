-- Derive the observation population from the sealed snapshot; search parameters stay private to retrieval.
ALTER TABLE browser_admission ADD COLUMN decision_allowed boolean NOT NULL DEFAULT false;
CREATE FUNCTION decision_admitted(w uuid,s uuid) RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT candidate_admitted(w,s) AND EXISTS(SELECT FROM snapshot sn JOIN import_batch b ON
 (b.workspace_id,b.source_id,b.id)=(w,sn.source_id,sn.batch_id) JOIN browser_admission a
 ON (a.workspace_id,a.source_id,a.checksum)=(w,b.source_id,b.file_sha256)
 WHERE (sn.workspace_id,sn.id)=(w,s) AND a.decision_allowed)
$$;
CREATE FUNCTION decision_scope(w uuid,src uuid,s uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT candidate_scope(w,src,s) THEN RETURN false; END IF;
 PERFORM FROM snapshot sn JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,src,b.file_sha256)
 WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s) AND a.decision_allowed FOR SHARE OF a;
 RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION decision_admitted(uuid,uuid),decision_scope(uuid,uuid,uuid) FROM PUBLIC;

-- Preserve the business seal when the new permission is false on all pre-existing file grants.
CREATE OR REPLACE FUNCTION retention_access_seal() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT CASE WHEN count(*)=0 THEN retention_access_seal_v6() ELSE encode(sha256(convert_to(retention_access_seal_v6() || string_agg(v,'' ORDER BY v),'UTF8')),'hex') END
 FROM (SELECT (CASE WHEN a.decision_allowed THEN to_jsonb(a) ELSE to_jsonb(a)-'decision_allowed' END)::text v FROM browser_admission a UNION ALL
 SELECT jsonb_build_array(sl.workspace_id,sl.source_id,sl.project_id,sl.location_id,sl.external_place_id,
 s.kind,s.source_key,s.external_source_id)::text FROM source_location sl JOIN source s
 ON (s.workspace_id,s.id)=(sl.workspace_id,sl.source_id)
 WHERE EXISTS(SELECT FROM browser_admission a WHERE (a.workspace_id,a.source_id)=(s.workspace_id,s.id))) grants
$$;
ALTER TABLE analysis_run ADD UNIQUE(workspace_id,id,snapshot_id);
CREATE TABLE decision_binding (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, project_id uuid NOT NULL, source_id uuid NOT NULL,
 snapshot_id uuid NOT NULL, aspect_id uuid NOT NULL, version_id uuid NOT NULL, digest text NOT NULL,
 profile text NOT NULL CHECK(profile='aspect-linear-lexical-v1'), threshold_set text NOT NULL CHECK(threshold_set='calibrated-v1'),
 profile_sha256 sha256_hex NOT NULL, session_binding sha256_hex NOT NULL, manifest jsonb,
 PRIMARY KEY(workspace_id,run_id), UNIQUE(workspace_id,run_id,snapshot_id,source_id),
 FOREIGN KEY(workspace_id,run_id,snapshot_id) REFERENCES analysis_run(workspace_id,id,snapshot_id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,source_id,snapshot_id) REFERENCES snapshot(workspace_id,source_id,id),
 FOREIGN KEY(workspace_id,project_id,source_id) REFERENCES source(workspace_id,project_id,id),
 FOREIGN KEY(workspace_id,project_id,aspect_id,version_id,digest) REFERENCES aspect_version(workspace_id,project_id,aspect_id,version_id,digest)
);
CREATE INDEX decision_catalog_idx ON decision_binding(workspace_id,source_id,snapshot_id,aspect_id,version_id);
CREATE TYPE decision_outcome AS ENUM ('complaint','mixed','denial','positive','neutral','unclear','not_mentioned','topic_uncertain','excluded_blank');
CREATE TABLE decision_row (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, snapshot_id uuid NOT NULL, source_id uuid NOT NULL, version_id uuid NOT NULL,
 generation integer NOT NULL CHECK (generation BETWEEN 1 AND 3),
 content_sha256 sha256_hex NOT NULL, text_sha256 sha256_hex NOT NULL, outcome decision_outcome NOT NULL,
 "start" integer, "end" integer, counter_start integer, counter_end integer, score double precision, reason text NOT NULL CHECK(reason IN ('MODEL_CONTEXT','LOW_MARGIN','DISJOINT_STANCES','ALL_WINDOWS_OFF_TOPIC','BLANK')),
 CHECK(num_nonnulls("start","end") IN (0,2) AND "start">=0 AND "end">"start"),
 CHECK(num_nonnulls(counter_start,counter_end) IN (0,2) AND counter_start>=0 AND counter_end>counter_start),
 CHECK((counter_start IS NOT NULL)=(reason='DISJOINT_STANCES') AND
       (counter_start IS NULL OR outcome='mixed' AND (counter_end<="start" OR "end"<=counter_start))),
 CHECK(score > '-Infinity'::float8 AND score < 'Infinity'::float8),
 CHECK(CASE WHEN outcome IN ('not_mentioned','excluded_blank') THEN "start" IS NULL
            WHEN outcome='topic_uncertain' THEN true ELSE "start" IS NOT NULL END),
 PRIMARY KEY(workspace_id,run_id,version_id),
 FOREIGN KEY(workspace_id,run_id,snapshot_id,source_id) REFERENCES decision_binding(workspace_id,run_id,snapshot_id,source_id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,source_id,version_id) REFERENCES review_version(workspace_id,source_id,id)
);
CREATE INDEX decision_outcome_page ON decision_row(workspace_id,run_id,outcome,version_id);
ALTER TABLE decision_binding ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_row ENABLE ROW LEVEL SECURITY;

CREATE FUNCTION decision_catalog(w uuid,src uuid,s uuid,a uuid,v uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('definition',d.def,'runs',coalesce((SELECT jsonb_agg(to_jsonb(t) ORDER BY created_at DESC,id) FROM (
    SELECT j.id,j.status,j.created_at FROM decision_binding b JOIN analysis_run j ON (j.workspace_id,j.id)=(w,b.run_id)
    WHERE (b.workspace_id,b.source_id,b.snapshot_id,b.aspect_id,b.version_id)=(w,src,s,a,v) ORDER BY j.created_at DESC,j.id LIMIT 25
 ) t),'[]'::jsonb)) FROM source z CROSS JOIN LATERAL (SELECT aspect_detail(w,z.project_id,a,v) def OFFSET 0) d
 WHERE (z.workspace_id,z.id)=(w,src) AND decision_scope(w,src,s) AND d.def IS NOT NULL
$$;

CREATE FUNCTION enqueue_decision(w uuid,src uuid,s uuid,a uuid,v uuid,k uuid,model text)
RETURNS TABLE(code integer,run_id uuid) LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE permitted integer:=job_writer(w); prior analysis_run; d aspect_version; archived boolean; session_key text;
BEGIN
 IF permitted<>200 THEN RETURN QUERY SELECT permitted,NULL::uuid; RETURN; END IF;
 IF k IS NULL OR model IS DISTINCT FROM 'aspect-linear-lexical-v1' THEN RETURN QUERY SELECT 422,NULL::uuid; RETURN; END IF;
 IF NOT decision_scope(w,src,s) THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
 SELECT av.* INTO d FROM source z JOIN aspect_version av ON (av.workspace_id,av.project_id)=(w,z.project_id)
 WHERE (z.workspace_id,z.id,av.aspect_id,av.version_id)=(w,src,a,v) AND aspect_scope(w,z.project_id);
 IF NOT FOUND THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
 SELECT h.archived_at IS NOT NULL INTO archived FROM aspect h WHERE (h.workspace_id,h.project_id,h.aspect_id)=(w,d.project_id,a) FOR SHARE;
 SELECT encode(sha256(x.token_hash||uuid_send(k)),'hex') INTO STRICT session_key FROM access_session x
 WHERE x.principal_id=access_actor() AND x.token_hash=sha256(convert_to(current_setting('rs.session_proof',true),'UTF8'));
 SELECT * INTO prior FROM analysis_run WHERE workspace_id=w AND request_key=k;
 IF FOUND THEN
   IF prior.requested_by=access_actor() AND EXISTS(SELECT FROM decision_binding b
    WHERE (b.workspace_id,b.run_id,b.source_id,b.snapshot_id,b.aspect_id,b.version_id,b.profile,b.session_binding)=(w,prior.id,src,s,a,v,model,session_key))
   THEN RETURN QUERY SELECT 200,prior.id; ELSE RETURN QUERY SELECT 409,NULL::uuid; END IF;
   RETURN;
 END IF;
 IF archived THEN RETURN QUERY SELECT 409,NULL::uuid; RETURN; END IF;
 IF (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND status IN ('queued','running'))>=4 OR
    (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND created_at>clock_timestamp()-interval '1 hour')>=20
 THEN RETURN QUERY SELECT 429,NULL::uuid; RETURN; END IF;
 INSERT INTO analysis_run(workspace_id,snapshot_id,job_kind,request_key,requested_by,engine_version,definition_version,model_version,stage,ready_at)
 VALUES(w,s,'snapshot_aspect_decision_v1',k,access_actor(),'1','aspect-decision-1',model,'queued',clock_timestamp()) RETURNING id INTO run_id;
 INSERT INTO decision_binding(workspace_id,run_id,project_id,source_id,snapshot_id,aspect_id,version_id,digest,profile,threshold_set,profile_sha256,session_binding)
 VALUES(w,run_id,d.project_id,src,s,a,v,d.digest,model,'calibrated-v1',decision_profile_sha(),session_key);
 code:=202; RETURN NEXT;
END $$;

CREATE FUNCTION decision_profile_sha() RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT 'f1daec7880fc4cfdafd119a5e0196053a26adfd38c55f8ce4cf21953459d57fd'::text
$$;

ALTER FUNCTION job_authorized(analysis_run) RENAME TO pre_decision_authorized;
CREATE FUNCTION job_authorized(j analysis_run) RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT pre_decision_authorized(j) AND (j.job_kind IS DISTINCT FROM 'snapshot_aspect_decision_v1' OR
 decision_admitted(j.workspace_id,j.snapshot_id) AND EXISTS(SELECT FROM decision_binding b JOIN access_session x
 ON x.principal_id=j.requested_by AND x.revoked_at IS NULL AND x.expires_at>statement_timestamp()
 WHERE (b.workspace_id,b.run_id)=(j.workspace_id,j.id) AND aspect_project_allowed(b.workspace_id,b.project_id)
 AND encode(sha256(x.token_hash||uuid_send(j.request_key)),'hex')=b.session_binding))
$$;
CREATE FUNCTION decision_authority(j analysis_run) RETURNS boolean LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE b decision_binding;
BEGIN
 SELECT * INTO STRICT b FROM decision_binding WHERE (workspace_id,run_id)=(j.workspace_id,j.id);
 PERFORM FROM workspace WHERE id=j.workspace_id AND state='active' FOR SHARE;
 IF NOT FOUND THEN RETURN false; END IF;
 PERFORM FROM client_project WHERE (workspace_id,id)=(b.workspace_id,b.project_id) FOR UPDATE;
 PERFORM FROM principal p JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(b.workspace_id,p.id)
 JOIN access_session x ON x.principal_id=p.id JOIN snapshot s ON (s.workspace_id,s.source_id,s.id)=(b.workspace_id,b.source_id,b.snapshot_id)
 JOIN import_batch i ON (i.workspace_id,i.source_id,i.id)=(b.workspace_id,b.source_id,s.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(b.workspace_id,b.source_id,i.file_sha256)
 WHERE p.id=j.requested_by AND p.state='active' AND m.state='active' AND m.role IN ('owner','analyst')
 AND x.revoked_at IS NULL AND x.expires_at>clock_timestamp() AND a.evidence_allowed AND a.candidate_allowed AND a.decision_allowed
 AND encode(sha256(x.token_hash||uuid_send(j.request_key)),'hex')=b.session_binding FOR SHARE OF p,m,x,a;
 RETURN FOUND AND job_authorized(j);
END $$;

ALTER FUNCTION job_input(uuid,uuid,uuid) RENAME TO pre_decision_input;
REVOKE EXECUTE ON FUNCTION pre_decision_input(uuid,uuid,uuid) FROM rs_worker;
CREATE FUNCTION job_input(w uuid,r uuid,t uuid) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE info jsonb:=pre_decision_input(w,r,t); params jsonb; eligible integer; same_members boolean; snap_id uuid; src_id uuid; batch_id_exact uuid; expected uuid[];
BEGIN
 IF info->>'kind' IS DISTINCT FROM 'snapshot_aspect_decision_v1' THEN RETURN info; END IF;
 SELECT jsonb_build_object('project_id',b.project_id,'aspect_id',b.aspect_id,'version_id',b.version_id,'digest',b.digest,
  'origin',av.origin,'payload',av.payload,'profile',b.profile,'profile_sha256',b.profile_sha256,'threshold_set',b.threshold_set)
 INTO params FROM decision_binding b JOIN aspect_version av ON av.version_id=b.version_id WHERE (b.workspace_id,b.run_id)=(w,r);
 -- Canonical import records the original missing_text decision on each accepted position.
 -- Check the membership seal here; the worker and decision_stage independently check the actual text.
 snap_id:=(info->>'snapshot_id')::uuid; src_id:=(info->>'source')::uuid; batch_id_exact:=(info->>'batch')::uuid;
 SELECT version_ids INTO STRICT expected FROM snapshot WHERE (workspace_id,id)=(w,snap_id);
 SELECT count(*) FILTER(WHERE NOT ('missing_text'=ANY(ir.warnings))),
   coalesce(array_agg(ir.version_id ORDER BY ir.version_id),'{}'::uuid[])=expected
 INTO eligible,same_members FROM import_row ir WHERE (ir.workspace_id,ir.source_id,ir.batch_id)=(w,src_id,batch_id_exact)
 AND ir.status='accepted';
 RETURN info||jsonb_build_object('decision',params||jsonb_build_object('eligible',eligible),
  'valid',(info->>'valid')::bool AND same_members);
END $$;

-- Acquire mutable authority in the same order as enqueue, then recheck and lock the exact attempt.
CREATE FUNCTION decision_fence(w uuid,r uuid,t uuid) RETURNS analysis_run LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run;
BEGIN
 IF NOT coalesce(retention_online(),false) THEN RETURN NULL; END IF;
 SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t) AND job_kind='snapshot_aspect_decision_v1';
 IF NOT FOUND OR NOT decision_authority(j) THEN RETURN NULL; END IF;
 SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t)
 AND status='running' AND NOT cancel_requested AND lease_until>clock_timestamp() FOR UPDATE;
 RETURN j;
END $$;

CREATE FUNCTION decision_stage(w uuid,r uuid,t uuid,items jsonb) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run:=decision_fence(w,r,t); n integer;
BEGIN
 IF j.id IS NULL THEN RETURN 0; END IF;
 IF jsonb_typeof(items) IS DISTINCT FROM 'array' OR octet_length(items::text)>65536 OR jsonb_array_length(items) NOT BETWEEN 1 AND 64
 THEN RAISE EXCEPTION 'invalid_decision_batch'; END IF;
 IF EXISTS(SELECT FROM jsonb_array_elements(items) item WHERE NOT item ?& ARRAY['version_id','content_sha256','text_sha256','outcome','start','end','score','reason']
 OR item-ARRAY['version_id','content_sha256','text_sha256','outcome','start','end','counter_start','counter_end','score','reason']<>'{}'::jsonb
 OR jsonb_typeof(item->'score') NOT IN ('null','number') OR jsonb_typeof(item->'start') NOT IN ('null','number')
 OR jsonb_typeof(item->'end') NOT IN ('null','number'))
 THEN RAISE EXCEPTION 'invalid_decision_row'; END IF;
 INSERT INTO decision_row(workspace_id,run_id,snapshot_id,source_id,version_id,generation,content_sha256,text_sha256,outcome,"start","end",counter_start,counter_end,score,reason)
 SELECT w,r,j.snapshot_id,m.source_id,v.id,j.attempt,v.content_sha256,x.text_sha256,x.outcome,x."start",x."end",x.counter_start,x.counter_end,x.score,x.reason
 FROM jsonb_to_recordset(items) AS x(version_id uuid,content_sha256 text,text_sha256 text,outcome decision_outcome,"start" integer,"end" integer,counter_start integer,counter_end integer,score float8,reason text)
 JOIN LATERAL (SELECT source_id,version_id FROM snapshot_member WHERE (workspace_id,snapshot_id,version_id)=(w,j.snapshot_id,x.version_id) LIMIT 1) m ON true
 JOIN LATERAL (SELECT id,text_content,content_sha256 FROM review_version WHERE (workspace_id,source_id,id)=(w,m.source_id,m.version_id) LIMIT 1) v ON true
 WHERE x.content_sha256=v.content_sha256 AND x.text_sha256=encode(sha256(convert_to(coalesce(v.text_content,''),'UTF8')),'hex')
 AND (x.outcome='excluded_blank')=NOT coalesce(aspect_nonblank(v.text_content),false)
 AND (x."end" IS NULL OR x."end"<=length(v.text_content)) AND (x.counter_end IS NULL OR x.counter_end<=length(v.text_content));
 GET DIAGNOSTICS n = ROW_COUNT;
 IF n<>jsonb_array_length(items) THEN RAISE EXCEPTION 'invalid_decision_membership'; END IF;
 IF j.lease_until<=clock_timestamp() THEN RAISE EXCEPTION 'stale_decision_attempt'; END IF;
 RETURN n;
END $$;

CREATE FUNCTION decision_document(w uuid,r uuid) RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE b decision_binding; sn snapshot; receipt import_batch; attempt_no integer;
 counts jsonb; row_count integer; eligible integer; mentions integer; complaints integer; unclear integer; rows_sha text;
BEGIN
 SELECT * INTO STRICT b FROM decision_binding WHERE (workspace_id,run_id)=(w,r);
 SELECT * INTO STRICT sn FROM snapshot WHERE (workspace_id,id)=(w,b.snapshot_id);
 SELECT * INTO STRICT receipt FROM import_batch WHERE (workspace_id,source_id,id)=(w,b.source_id,sn.batch_id);
 SELECT attempt INTO STRICT attempt_no FROM analysis_run WHERE (workspace_id,id)=(w,r);
 SELECT count(*),encode(sha256(convert_to(coalesce(string_agg(encode(sha256(convert_to(jsonb_build_array(
       x.version_id,x.content_sha256,x.text_sha256,x.outcome,x."start",x."end",x.counter_start,x.counter_end,x.score,x.reason)::text,'UTF8')),'hex'),
       '' ORDER BY x.version_id),''),'UTF8')),'hex') INTO row_count,rows_sha
 FROM decision_row x WHERE (x.workspace_id,x.run_id,x.generation)=(w,r,attempt_no);
 -- Validate the full version set independently of batch insertion and its indexed membership check.
 IF row_count IS DISTINCT FROM receipt.accepted_count OR row_count<>cardinality(sn.version_ids)
 OR sn.version_ids IS DISTINCT FROM ARRAY(SELECT version_id FROM decision_row WHERE (workspace_id,run_id)=(w,r) ORDER BY version_id)
 OR sn.version_ids IS DISTINCT FROM ARRAY(SELECT ir.version_id FROM import_row ir WHERE
  (ir.workspace_id,ir.source_id,ir.batch_id)=(w,b.source_id,sn.batch_id) AND ir.status='accepted' ORDER BY ir.version_id)
 OR receipt.input_count IS DISTINCT FROM row_count+receipt.rejected_count+receipt.confirmed_duplicate_count
 THEN RAISE EXCEPTION 'incomplete_decision_membership'; END IF;
 SELECT jsonb_object_agg(k,coalesce(n,0)) INTO counts FROM unnest(enum_range(NULL::decision_outcome)) k LEFT JOIN
 (SELECT outcome,count(*) n FROM decision_row WHERE (workspace_id,run_id)=(w,r) GROUP BY outcome) g ON g.outcome=k;
 eligible:=row_count-(counts->>'excluded_blank')::int;
 mentions:=eligible-(counts->>'not_mentioned')::int-(counts->>'topic_uncertain')::int;
 complaints:=(counts->>'complaint')::int+(counts->>'mixed')::int;
 unclear:=(counts->>'unclear')::int+(counts->>'topic_uncertain')::int;
 RETURN jsonb_build_object('schema','aspect-decision-1','workspace_id',w,'project_id',b.project_id,'source_id',b.source_id,
 'snapshot_id',b.snapshot_id,'run_id',r,'manifest_sha256',sn.manifest_sha256,'snapshot_sha256',sn.duckdb_sha256,
 'aspect_id',b.aspect_id,'version_id',b.version_id,'digest',b.digest,'profile',b.profile,'threshold_set',b.threshold_set,
 'profile_sha256',b.profile_sha256,'rows_sha256',rows_sha,'summary',jsonb_build_object(
 'N_input',receipt.input_count,'N_accepted',row_count,'N_quarantine',receipt.rejected_count,'N_confirmed_duplicate',receipt.confirmed_duplicate_count,
 'N_excluded',row_count-eligible,'N_eligible',eligible,'N_evaluated',eligible,'N_not_evaluated',0,'N_not_mentioned',counts->'not_mentioned',
 'N_topic_uncertain',counts->'topic_uncertain','N_mentioned',mentions,'N_complaints',complaints,'N_mentioned_unclear',counts->'unclear',
 'N_mentioned_other',mentions-complaints-(counts->>'unclear')::int,'N_uncertain',unclear,'by_outcome',counts,
 'coverage',eligible::double precision/nullif(eligible,0),'defined_coverage',(eligible-unclear)::double precision/nullif(eligible,0),
 'complaints_among_eligible',complaints::double precision/nullif(eligible,0),'complaints_among_mentions',complaints::double precision/nullif(mentions,0),
 'undefined_rates',jsonb_strip_nulls(jsonb_build_object('coverage',CASE WHEN eligible=0 THEN 'NO_ELIGIBLE_REVIEWS' END,
 'defined_coverage',CASE WHEN eligible=0 THEN 'NO_ELIGIBLE_REVIEWS' END,
 'complaints_among_eligible',CASE WHEN eligible=0 THEN 'NO_ELIGIBLE_REVIEWS' END,
 'complaints_among_mentions',CASE WHEN mentions=0 THEN 'NO_MENTIONS' END))));
END $$;

CREATE FUNCTION decision_manifest(w uuid,r uuid,t uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF (decision_fence(w,r,t)).id IS NULL THEN RETURN NULL; END IF;
 RETURN decision_document(w,r);
END $$;
CREATE FUNCTION decision_runtime_valid(d jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT coalesce(jsonb_typeof(d)='object' AND d ?& ARRAY['python','numpy','sklearn','dtype']
   AND d-ARRAY['python','numpy','sklearn','dtype']='{}'::jsonb AND
   (d=jsonb_build_object('python','0','numpy','0','sklearn','0','dtype','test-double') OR
    d->>'dtype'='float64' AND NOT EXISTS(SELECT FROM unnest(ARRAY['python','numpy','sklearn']) k
        WHERE jsonb_typeof(d->k)<>'string' OR d->>k !~ '^[0-9]{1,3}([.][0-9]{1,3}){1,3}$')),false)
$$;
REVOKE ALL ON FUNCTION decision_runtime_valid(jsonb) FROM PUBLIC;
CREATE FUNCTION verify_decision_publication() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run; raw text; doc jsonb;
BEGIN
 SELECT * INTO STRICT j FROM analysis_run WHERE (workspace_id,id)=(NEW.workspace_id,NEW.run_id);
 IF j.job_kind IS DISTINCT FROM 'snapshot_aspect_decision_v1' THEN RETURN NEW; END IF;
 IF session_user<>'rs_worker' OR (decision_fence(j.workspace_id,j.id,j.lease_token)).id IS NULL THEN RAISE EXCEPTION 'decision_refused'; END IF;
 raw:=current_setting('rs.decision_projection',true);
 IF raw IS NULL OR octet_length(raw)<>NEW.size_bytes OR NEW.size_bytes NOT BETWEEN 1 AND 65536
 OR encode(sha256(convert_to(raw,'UTF8')),'hex')<>NEW.checksum THEN RAISE EXCEPTION 'decision_hash_mismatch'; END IF;
 doc:=decision_document(j.workspace_id,j.id);
 IF doc IS DISTINCT FROM raw::jsonb-'runtime' OR NOT decision_runtime_valid(raw::jsonb->'runtime') OR j.processed IS DISTINCT FROM (doc#>>'{summary,N_evaluated}')::integer
 THEN RAISE EXCEPTION 'decision_manifest_mismatch'; END IF;
 UPDATE decision_binding SET manifest=raw::jsonb WHERE (workspace_id,run_id)=(j.workspace_id,j.id) AND manifest IS NULL;
 IF NOT FOUND OR j.lease_until<=clock_timestamp() THEN RAISE EXCEPTION 'decision_publication_refused'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER verify_decision_publication BEFORE INSERT ON artifact FOR EACH ROW EXECUTE FUNCTION verify_decision_publication();
CREATE FUNCTION guard_decision_binding() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF session_user='rs_worker' AND OLD.manifest IS NULL AND NEW.manifest IS NOT NULL
 AND (to_jsonb(NEW)-'manifest')=(to_jsonb(OLD)-'manifest') THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'immutable_decision_binding';
END $$;
CREATE TRIGGER decision_binding_update BEFORE UPDATE ON decision_binding FOR EACH ROW EXECUTE FUNCTION guard_decision_binding();
CREATE TRIGGER decision_binding_delete BEFORE DELETE ON decision_binding FOR EACH ROW EXECUTE FUNCTION reject_history_change();
CREATE TRIGGER decision_row_update BEFORE UPDATE ON decision_row FOR EACH ROW EXECUTE FUNCTION reject_history_change();

CREATE FUNCTION guard_decision_delete() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF pg_trigger_depth()>1 AND EXISTS(SELECT FROM analysis_run j WHERE (j.workspace_id,j.id)=(OLD.workspace_id,OLD.run_id)
   AND j.status IN ('failed','cancelled','queued')) THEN RETURN OLD; END IF;
 IF session_user='rs_retention' AND EXISTS(SELECT FROM retention_request q
   WHERE q.id=nullif(current_setting('rs.erasure',true),'')::uuid AND q.workspace_id=OLD.workspace_id
    AND q.state='revoking' AND q.action='erase' AND OLD.source_id IN (SELECT retention_sources(q.workspace_id,q.kind,q.target)))
 THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'immutable_decision_rows' USING ERRCODE='23514';
END $$;
REVOKE ALL ON FUNCTION guard_decision_delete() FROM PUBLIC;
CREATE TRIGGER decision_row_delete BEFORE DELETE ON decision_row FOR EACH ROW EXECUTE FUNCTION guard_decision_delete();
CREATE TRIGGER immutable_table BEFORE TRUNCATE ON decision_row FOR EACH STATEMENT EXECUTE FUNCTION reject_history_change();
CREATE TRIGGER immutable_table BEFORE TRUNCATE ON decision_binding FOR EACH STATEMENT EXECUTE FUNCTION reject_history_change();

ALTER TABLE analysis_run DROP CONSTRAINT analysis_run_reason_check;
ALTER TABLE analysis_run ADD CONSTRAINT analysis_run_reason_check CHECK(reason IN ('CANCELLED','ACCESS_REVOKED','LEASE_EXPIRED',
 'STORAGE_UNAVAILABLE','WORKER_STOPPED','INTEGRITY_ERROR','UNSUPPORTED_FORMAT','RESOURCE_LIMIT','UNSUPPORTED_VERSION','INTERNAL_ERROR')
 OR (job_kind IS NOT DISTINCT FROM 'snapshot_aspect_decision_v1' AND reason IN ('MODEL_UNAVAILABLE','UNSUPPORTED_DEFINITION','INVALID_PROFILE')));
ALTER FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) RENAME TO pre_decision_step;
REVOKE EXECUTE ON FUNCTION pre_decision_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM rs_worker;
CREATE FUNCTION worker_step(w uuid,r uuid,t uuid,next_stage text,n integer,den integer,
 problem text DEFAULT NULL,digest sha256_hex DEFAULT NULL,bytes integer DEFAULT NULL,manifest sha256_hex DEFAULT NULL)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run; valid boolean;
BEGIN
 IF NOT coalesce(retention_online(),false) THEN RETURN 'stale'; END IF;
 SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t) AND job_kind='snapshot_aspect_decision_v1';
 IF FOUND THEN
   valid:=decision_authority(j);
   SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t) AND status='running' AND lease_until>clock_timestamp() FOR UPDATE;
   IF NOT FOUND THEN RETURN 'stale'; END IF;
   IF j.cancel_requested THEN RETURN stop_attempt(j,'CANCELLED'); END IF;
   IF NOT valid THEN RETURN stop_attempt(j,'ACCESS_REVOKED'); END IF;
   IF problem IN ('MODEL_UNAVAILABLE','UNSUPPORTED_DEFINITION','INVALID_PROFILE') THEN RETURN stop_attempt(j,problem); END IF;
 END IF;
 RETURN pre_decision_step(w,r,t,next_stage,n,den,problem,digest,bytes,manifest);
END $$;
CREATE FUNCTION clear_decision_stage() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 DELETE FROM decision_row WHERE (workspace_id,run_id)=(NEW.workspace_id,NEW.id);
 RETURN NULL;
END $$;
CREATE TRIGGER clear_decision_stage AFTER UPDATE OF status ON analysis_run FOR EACH ROW
 WHEN (OLD.job_kind='snapshot_aspect_decision_v1' AND OLD.status='running' AND NEW.status IN ('failed','cancelled','queued'))
 EXECUTE FUNCTION clear_decision_stage();
CREATE FUNCTION decision_read(w uuid,src uuid,s uuid,r uuid,f text,skip integer,detail uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j analysis_run; b decision_binding; a artifact; output jsonb; listed jsonb; n integer; label decision_outcome;
BEGIN
 IF skip IS NULL OR skip NOT BETWEEN 0 AND 10000 OR (f IS NOT NULL AND f<>ALL(enum_range(NULL::decision_outcome)::text[]))
 OR NOT decision_scope(w,src,s) THEN RETURN NULL; END IF;
 SELECT * INTO b FROM decision_binding WHERE (workspace_id,source_id,snapshot_id,run_id)=(w,src,s,r);
 IF NOT FOUND OR NOT aspect_scope(w,b.project_id) THEN RETURN NULL; END IF;
 SELECT * INTO STRICT j FROM analysis_run WHERE (workspace_id,id)=(w,r);
 output:=jsonb_build_object('id',r,'status',j.status,'reason',j.reason,'stage',j.stage,'processed',j.processed,'total',j.total,
 'compute_state','NOT_COMPUTED','summary',NULL,'filtered_total',NULL,'rows','[]'::jsonb,'profile',b.profile,'threshold_set',b.threshold_set,
 'profile_sha256',b.profile_sha256,'definition',aspect_detail(w,b.project_id,b.aspect_id,b.version_id));
 IF j.status<>'succeeded' THEN RETURN CASE WHEN detail IS NULL THEN output END; END IF;
 SELECT * INTO a FROM artifact WHERE (workspace_id,run_id,id)=(w,r,j.output_artifact_id) AND storage_state='available';
 IF NOT FOUND OR b.manifest IS NULL THEN RETURN NULL; END IF;
 label:=f::decision_outcome;
 n:=CASE WHEN f IS NULL THEN (b.manifest#>>'{summary,N_accepted}')::int ELSE (b.manifest#>>ARRAY['summary','by_outcome',f])::int END;
 -- Separate query shapes keep the point lookup indexable under a generic cached plan.
 WITH hits AS MATERIALIZED (
   (SELECT d.* FROM decision_row d WHERE detail IS NOT NULL AND (workspace_id,run_id,version_id)=(w,r,detail)
    AND (label IS NULL OR outcome=label))
   UNION ALL
   (SELECT d.* FROM decision_row d WHERE detail IS NULL AND label IS NULL AND (workspace_id,run_id)=(w,r)
    ORDER BY version_id LIMIT 25 OFFSET skip)
   UNION ALL
   (SELECT d.* FROM decision_row d WHERE detail IS NULL AND label IS NOT NULL AND (workspace_id,run_id,outcome)=(w,r,label)
    ORDER BY version_id LIMIT 25 OFFSET skip)
 )
 SELECT coalesce(jsonb_agg(to_jsonb(q) ORDER BY q.version_id),'[]'::jsonb) INTO listed FROM
 (SELECT version_id,content_sha256,text_sha256,outcome,"start","end",counter_start,counter_end,score,reason FROM hits) q;
 IF detail IS NOT NULL AND jsonb_array_length(listed)<>1 THEN RETURN NULL; END IF;
 RETURN output||jsonb_build_object('compute_state','COMPLETE','summary',b.manifest->'summary','filtered_total',n,'rows',listed,
 'artifact_sha256',a.checksum,'runtime',b.manifest->'runtime','rows_sha256',b.manifest->'rows_sha256','manifest_sha256',b.manifest->'manifest_sha256');
END $$;

ALTER FUNCTION retention_plan(uuid,text,uuid) RENAME TO pre_decision_retention_plan;
CREATE FUNCTION retention_plan(w uuid,k text,t uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 WITH result AS (SELECT pre_decision_retention_plan(w,k,t) doc),
 bindings AS MATERIALIZED (SELECT run_id FROM decision_binding WHERE workspace_id=w AND source_id IN (SELECT retention_sources(w,k,t)))
 SELECT jsonb_set(doc,'{tables}',(doc->'tables')||jsonb_build_object('decision_binding',(SELECT count(*) FROM bindings),
 'decision_row',(SELECT count(*) FROM decision_row WHERE workspace_id=w AND run_id IN (SELECT run_id FROM bindings)))) FROM result
$$;
CREATE FUNCTION decision_exportable(w uuid,r uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM decision_binding b WHERE (b.workspace_id,b.run_id)=(w,r) AND candidate_exportable(w,b.snapshot_id) AND decision_admitted(w,b.snapshot_id)
  AND aspect_project_allowed(w,b.project_id))
$$;
CREATE POLICY decision_export ON analysis_run AS RESTRICTIVE FOR SELECT TO rs_retention
 USING(job_kind IS DISTINCT FROM 'snapshot_aspect_decision_v1' OR decision_exportable(workspace_id,id));
CREATE POLICY owner_export ON decision_binding FOR SELECT TO rs_retention USING(decision_exportable(workspace_id,run_id));
CREATE POLICY owner_export ON decision_row FOR SELECT TO rs_retention USING(EXISTS(SELECT FROM decision_binding b
 WHERE b.workspace_id=decision_row.workspace_id AND b.run_id=decision_row.run_id AND b.manifest IS NOT NULL));
GRANT SELECT(workspace_id,run_id,project_id,source_id,snapshot_id,aspect_id,version_id,digest,profile,threshold_set,profile_sha256,manifest)
 ON decision_binding TO rs_retention;
GRANT SELECT(workspace_id,run_id,snapshot_id,version_id,content_sha256,text_sha256,outcome,"start","end",counter_start,counter_end,score,reason) ON decision_row TO rs_retention;

REVOKE ALL ON FUNCTION decision_profile_sha(),decision_catalog(uuid,uuid,uuid,uuid,uuid),enqueue_decision(uuid,uuid,uuid,uuid,uuid,uuid,text),
 job_authorized(analysis_run),decision_authority(analysis_run),job_input(uuid,uuid,uuid),decision_fence(uuid,uuid,uuid),
 decision_stage(uuid,uuid,uuid,jsonb),decision_manifest(uuid,uuid,uuid),decision_document(uuid,uuid),verify_decision_publication(),
 guard_decision_binding(),worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex),clear_decision_stage(),
 decision_read(uuid,uuid,uuid,uuid,text,integer,uuid),retention_plan(uuid,text,uuid),decision_exportable(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decision_exportable(uuid,uuid) TO rs_retention;
GRANT EXECUTE ON FUNCTION decision_catalog(uuid,uuid,uuid,uuid,uuid),enqueue_decision(uuid,uuid,uuid,uuid,uuid,uuid,text),
 decision_read(uuid,uuid,uuid,uuid,text,integer,uuid) TO rs_runtime;
GRANT EXECUTE ON FUNCTION job_input(uuid,uuid,uuid),decision_manifest(uuid,uuid,uuid),decision_stage(uuid,uuid,uuid,jsonb),
 worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) TO rs_worker;

CREATE FUNCTION decision_object(w uuid,src uuid,s uuid,r uuid,actor uuid,expires bigint,ticket text)
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
 AND decision_admitted(w,s) AND aspect_project_allowed(w,b.project_id) AND b.manifest IS NOT NULL AND j.status='succeeded' AND j.job_kind='snapshot_aspect_decision_v1'
 AND (j.engine_version,j.definition_version)=('1','aspect-decision-1')
 AND a.storage_state='available' AND a.size_bytes BETWEEN 1 AND 65536
 AND expires BETWEEN floor(extract(epoch FROM statement_timestamp()))::bigint
 AND floor(extract(epoch FROM statement_timestamp()))::bigint+30
 AND EXISTS(SELECT FROM access_session x WHERE x.principal_id=actor AND x.revoked_at IS NULL
 AND x.expires_at>statement_timestamp() AND encode(sha256(x.token_hash || sha256(convert_to(
 concat_ws('|','aspect-decision-1',w,src,s,r,actor,expires),'UTF8'))),'hex')=ticket)
$$;
REVOKE ALL ON FUNCTION decision_object(uuid,uuid,uuid,uuid,uuid,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decision_object(uuid,uuid,uuid,uuid,uuid,bigint,text) TO rs_upload;

-- Shared job metadata and cancellation also obey the new admission.
CREATE FUNCTION decision_visible(w uuid,r uuid) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM decision_binding b WHERE (b.workspace_id,b.run_id)=(w,r)
 AND decision_scope(w,b.source_id,b.snapshot_id) AND aspect_scope(w,b.project_id))
$$;
REVOKE ALL ON FUNCTION decision_visible(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decision_visible(uuid,uuid) TO rs_runtime;
CREATE POLICY decision_reads ON analysis_run AS RESTRICTIVE FOR SELECT TO rs_runtime
 USING(job_kind IS DISTINCT FROM 'snapshot_aspect_decision_v1' OR decision_visible(workspace_id,id));
ALTER FUNCTION cancel_job(uuid,uuid) RENAME TO pre_decision_cancel;
REVOKE EXECUTE ON FUNCTION pre_decision_cancel(uuid,uuid) FROM rs_runtime;
CREATE FUNCTION cancel_job(w uuid,r uuid) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE permitted integer:=job_writer(w);
BEGIN
 IF permitted<>200 THEN RETURN permitted; END IF;
 IF EXISTS(SELECT FROM decision_binding WHERE (workspace_id,run_id)=(w,r)) AND NOT decision_visible(w,r) THEN RETURN 404; END IF;
 RETURN pre_decision_cancel(w,r);
END $$;
REVOKE ALL ON FUNCTION cancel_job(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cancel_job(uuid,uuid) TO rs_runtime;
ALTER FUNCTION enqueue_job(uuid,uuid,uuid,text,text) RENAME TO pre_decision_enqueue;
REVOKE EXECUTE ON FUNCTION pre_decision_enqueue(uuid,uuid,uuid,text,text) FROM rs_runtime;
CREATE FUNCTION enqueue_job(w uuid,s uuid,k uuid,kind text,version text)
RETURNS TABLE(code integer,run_id uuid) LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF kind='snapshot_aspect_decision_v1' THEN RETURN QUERY SELECT 422,NULL::uuid; RETURN; END IF;
 RETURN QUERY SELECT * FROM pre_decision_enqueue(w,s,k,kind,version);
END $$;
REVOKE ALL ON FUNCTION enqueue_job(uuid,uuid,uuid,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION enqueue_job(uuid,uuid,uuid,text,text) TO rs_runtime;

CREATE FUNCTION decision_manifest_valid(w uuid,r uuid) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE saved jsonb;
BEGIN
 IF session_user='rs_retention' AND NOT decision_exportable(w,r) THEN RETURN false; END IF;
 SELECT b.manifest INTO saved FROM decision_binding b JOIN analysis_run j ON (j.workspace_id,j.id)=(w,r)
 WHERE (b.workspace_id,b.run_id)=(w,r) AND j.status='succeeded';
 IF saved IS NULL THEN RETURN false; END IF;
 RETURN saved-'runtime'=decision_document(w,r) AND decision_runtime_valid(saved->'runtime');
END $$;
REVOKE ALL ON FUNCTION decision_manifest_valid(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decision_manifest_valid(uuid,uuid) TO rs_retention;
