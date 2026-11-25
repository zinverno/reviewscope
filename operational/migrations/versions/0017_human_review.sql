-- Every permission is specific to the admitted bytes. Preserve the 0016 business seal when off.
ALTER TABLE browser_admission ADD COLUMN human_review_allowed boolean NOT NULL DEFAULT false;
CREATE OR REPLACE FUNCTION retention_access_seal() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT CASE WHEN count(*)=0 THEN retention_access_seal_v6() ELSE encode(sha256(convert_to(retention_access_seal_v6() || string_agg(v,'' ORDER BY v),'UTF8')),'hex') END
 FROM (SELECT (to_jsonb(a) - CASE WHEN a.decision_allowed THEN ARRAY[]::text[] ELSE ARRAY['decision_allowed'] END
                       - CASE WHEN a.human_review_allowed THEN ARRAY[]::text[] ELSE ARRAY['human_review_allowed'] END)::text v
 FROM browser_admission a UNION ALL
 SELECT jsonb_build_array(sl.workspace_id,sl.source_id,sl.project_id,sl.location_id,sl.external_place_id,
 s.kind,s.source_key,s.external_source_id)::text FROM source_location sl JOIN source s
 ON (s.workspace_id,s.id)=(sl.workspace_id,sl.source_id)
 WHERE EXISTS(SELECT FROM browser_admission a WHERE (a.workspace_id,a.source_id)=(s.workspace_id,s.id))) grants
$$;

CREATE TABLE human_review_plan (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
 request_key uuid NOT NULL, actor uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 seed uuid NOT NULL, requested int NOT NULL CHECK(requested BETWEEN 1 AND 10000),
 eligible int NOT NULL CHECK(eligible BETWEEN 0 AND 10000), size int NOT NULL CHECK(size=least(eligible,requested)),
 algorithm text NOT NULL DEFAULT 'human-sample-v1' CHECK(algorithm='human-sample-v1'), digest sha256_hex NOT NULL,
 PRIMARY KEY(workspace_id,run_id,id), UNIQUE(workspace_id,request_key),
 FOREIGN KEY(workspace_id,run_id) REFERENCES decision_binding ON DELETE CASCADE
);
CREATE TABLE human_plan_member (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, plan_id uuid NOT NULL, version_id uuid NOT NULL,
 ordinal int NOT NULL CHECK(ordinal BETWEEN 1 AND 10000),
 PRIMARY KEY(workspace_id,run_id,plan_id,version_id), UNIQUE(workspace_id,run_id,plan_id,ordinal),
 FOREIGN KEY(workspace_id,run_id,plan_id) REFERENCES human_review_plan ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,run_id,version_id) REFERENCES decision_row ON DELETE CASCADE
);

-- Reverse foreign-key lookups during scoped source erasure.
CREATE INDEX human_plan_member_version ON human_plan_member(workspace_id,run_id,version_id);

CREATE TABLE human_review_event (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, version_id uuid NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
 sequence bigint NOT NULL CHECK(sequence>0), previous_id uuid, request_key uuid NOT NULL,
 actor uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 action text NOT NULL CHECK(action IN ('confirm','correct','dispute')), outcome decision_outcome,
 reason text NOT NULL CHECK(length(reason)>=1 AND length(reason)<=300 AND aspect_nonblank(reason)),
 spans jsonb NOT NULL CHECK(jsonb_typeof(spans)='array' AND jsonb_array_length(spans)<=2),
 selection text NOT NULL CHECK(selection IN ('all','uncertain','disagreement','sample')), plan_id uuid,
 machine decision_outcome NOT NULL CHECK(machine<>'excluded_blank'),
 content_sha256 sha256_hex NOT NULL, text_sha256 sha256_hex NOT NULL,
 PRIMARY KEY(workspace_id,run_id,version_id,id), UNIQUE(workspace_id,run_id,version_id,sequence),
 UNIQUE(workspace_id,request_key), CHECK((previous_id IS NULL)=(sequence=1)),
 CHECK((selection='sample')=(plan_id IS NOT NULL)),
 CHECK((action='dispute')=(outcome IS NULL) AND outcome IS DISTINCT FROM 'excluded_blank'::decision_outcome),
 CHECK(CASE action WHEN 'confirm' THEN outcome=machine WHEN 'correct' THEN outcome<>machine ELSE true END),
 CHECK(outcome NOT IN ('mixed','complaint') OR jsonb_array_length(spans)>0),
 FOREIGN KEY(workspace_id,run_id,version_id) REFERENCES decision_row ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,run_id,version_id,previous_id) REFERENCES human_review_event DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(workspace_id,run_id,plan_id,version_id) REFERENCES human_plan_member DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE human_review_result (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
 request_key uuid NOT NULL, actor uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 algorithm text NOT NULL DEFAULT 'human-resolved-v1' CHECK(algorithm='human-resolved-v1'),
 digest sha256_hex NOT NULL, manifest_sha256 sha256_hex NOT NULL, rows_sha256 sha256_hex NOT NULL, artifact_sha256 sha256_hex NOT NULL,
 summary jsonb NOT NULL CHECK(jsonb_typeof(summary)='object'),
 PRIMARY KEY(workspace_id,run_id,id), UNIQUE(workspace_id,request_key),
 FOREIGN KEY(workspace_id,run_id) REFERENCES decision_binding ON DELETE CASCADE
);
CREATE TABLE human_result_member (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, result_id uuid NOT NULL, version_id uuid NOT NULL, event_id uuid NOT NULL,
 PRIMARY KEY(workspace_id,run_id,result_id,version_id),
 FOREIGN KEY(workspace_id,run_id,result_id) REFERENCES human_review_result ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(workspace_id,run_id,version_id,event_id) REFERENCES human_review_event DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX human_result_member_event ON human_result_member(workspace_id,run_id,version_id,event_id);

-- PR-22 validated the exact row set and immutable manifest at publication. The HTTP boundary
-- separately re-reads its checksum-bound object on every request; SQL never accepts a client digest.
CREATE FUNCTION human_scope(w uuid,src uuid,s uuid,r uuid,writing boolean) RETURNS boolean LANGUAGE plpgsql
SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE p uuid;
BEGIN
 SELECT b.project_id INTO p FROM decision_binding b JOIN analysis_run j ON (j.workspace_id,j.id)=(w,r)
 JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=(w,r,j.output_artifact_id)
 WHERE (b.workspace_id,b.source_id,b.snapshot_id,b.run_id)=(w,src,s,r) AND j.status='succeeded'
 AND j.job_kind='snapshot_aspect_decision_v1' AND (j.snapshot_id,b.version_id::text,b.digest)=(s,b.manifest->>'version_id',b.manifest->>'digest')
 AND b.manifest->>'rows_sha256' IS NOT NULL AND b.manifest->>'manifest_sha256' IS NOT NULL
 AND a.storage_state='available' AND a.size_bytes BETWEEN 1 AND 65536;
 IF NOT FOUND OR aspect_scope(w,p) IS NOT TRUE OR decision_scope(w,src,s) IS NOT TRUE THEN RETURN false; END IF;
 PERFORM FROM snapshot sn JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,src,b.file_sha256)
 WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s) AND a.human_review_allowed FOR SHARE OF a;
 IF NOT FOUND THEN RETURN false; END IF;
 IF writing THEN
   -- Every append and publication takes this same lock. A waiting writer gets a fresh snapshot for its CAS.
   PERFORM FROM decision_binding WHERE (workspace_id,run_id)=(w,r) FOR UPDATE;
 END IF;
 RETURN EXISTS(SELECT FROM access_session x WHERE x.token_hash=sha256(convert_to(current_setting('rs.session_proof',true),'UTF8'))
               AND x.revoked_at IS NULL AND x.expires_at>clock_timestamp());
END $$;

CREATE FUNCTION human_state(action text) RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT CASE action WHEN 'confirm' THEN 'confirmed' WHEN 'correct' THEN 'corrected' WHEN 'dispute' THEN 'disputed' ELSE 'unreviewed' END
$$;
CREATE FUNCTION human_status(code integer,id uuid DEFAULT NULL) RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('code',code,'id',id)
$$;

-- Serialize matching request keys across runs and operation types without disclosing the prior target.
CREATE FUNCTION human_request_kind(w uuid,k uuid) RETURNS text LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(w::text||k::text,627379));
 RETURN (SELECT 'event' WHERE EXISTS(SELECT FROM human_review_event WHERE workspace_id=w AND request_key=k)
 UNION ALL SELECT 'plan' WHERE EXISTS(SELECT FROM human_review_plan WHERE workspace_id=w AND request_key=k)
 UNION ALL SELECT 'result' WHERE EXISTS(SELECT FROM human_review_result WHERE workspace_id=w AND request_key=k));
END $$;

CREATE FUNCTION human_spans_valid(spans jsonb,original text) RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE el jsonb; lo integer; hi integer; prior_lo integer; prior_hi integer;
BEGIN
 IF jsonb_typeof(spans)<>'array' OR octet_length(spans::text)>256 THEN RETURN false; END IF;
 IF jsonb_array_length(spans)>2 THEN RETURN false; END IF;
 FOR el IN SELECT value FROM jsonb_array_elements(spans) LOOP
   IF jsonb_typeof(el)<>'object' OR NOT el ?& ARRAY['start','end'] OR el-ARRAY['start','end']<>'{}'
   OR jsonb_typeof(el->'start')<>'number' OR jsonb_typeof(el->'end')<>'number'
   OR el->>'start' !~ '^[0-9]{1,8}$' OR el->>'end' !~ '^[0-9]{1,8}$' THEN RETURN false; END IF;
   lo:=(el->>'start')::integer; hi:=(el->>'end')::integer;
   IF hi<=lo OR hi>length(original) OR NOT aspect_nonblank(substr(original,lo+1,hi-lo))
   OR (lo<prior_hi AND hi>prior_lo) THEN RETURN false; END IF;
   prior_lo:=lo; prior_hi:=hi;
 END LOOP;
 RETURN true;
END $$;

CREATE FUNCTION human_append(w uuid,src uuid,s uuid,r uuid,v uuid,k uuid,expected uuid,act text,label text,note text,
 evidence jsonb,mode text,plan uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE d decision_row; original text; prior human_review_event; who uuid; head human_review_event; saved uuid; kind text;
BEGIN
 IF NOT human_scope(w,src,s,r,true) THEN RETURN NULL; END IF;
 who:=access_actor();
 IF num_nulls(v,k,act,note,evidence,mode)>0 OR act NOT IN ('confirm','correct','dispute') OR mode NOT IN ('all','uncertain','disagreement','sample')
 OR (mode='sample')<>(plan IS NOT NULL) OR (act='dispute')<>(label IS NULL)
 OR (label IS NOT NULL AND (label='excluded_blank' OR label<>ALL(enum_range(NULL::decision_outcome)::text[])))
 OR length(note) NOT BETWEEN 1 AND 300 OR NOT aspect_nonblank(note) THEN RETURN human_status(422); END IF;
 kind:=human_request_kind(w,k);
 IF kind IS NOT NULL THEN
   SELECT * INTO prior FROM human_review_event WHERE (workspace_id,run_id,version_id,request_key)=(w,r,v,k);
   IF FOUND AND (prior.actor,prior.previous_id,prior.action,prior.outcome::text,prior.reason,prior.spans,prior.selection,prior.plan_id)
       IS NOT DISTINCT FROM (who,expected,act,label,note,evidence,mode,plan) THEN RETURN human_status(200,prior.id); END IF;
   RETURN human_status(409);
 END IF;
 SELECT * INTO d FROM decision_row WHERE (workspace_id,run_id,version_id)=(w,r,v);
 IF NOT FOUND OR d.outcome='excluded_blank' THEN RETURN NULL; END IF;
 SELECT text_content INTO original FROM review_version WHERE (workspace_id,source_id,id,content_sha256)=(w,src,v,d.content_sha256);
 IF NOT FOUND OR NOT coalesce(aspect_nonblank(original),false)
 OR encode(sha256(convert_to(original,'UTF8')),'hex')<>d.text_sha256 THEN RETURN NULL; END IF;
 IF NOT human_spans_valid(evidence,original) OR label IN ('mixed','complaint') AND jsonb_array_length(evidence)=0
 OR act='confirm' AND label<>d.outcome::text OR act='correct' AND label=d.outcome::text THEN RETURN human_status(422); END IF;
 IF plan IS NOT NULL AND NOT EXISTS(SELECT FROM human_plan_member WHERE (workspace_id,run_id,plan_id,version_id)=(w,r,plan,v)) THEN RETURN NULL; END IF;
 SELECT * INTO head FROM human_review_event WHERE (workspace_id,run_id,version_id)=(w,r,v) ORDER BY sequence DESC LIMIT 1;
 IF head.id IS DISTINCT FROM expected THEN RETURN human_status(409); END IF;
 IF mode='uncertain' AND d.outcome NOT IN ('topic_uncertain','unclear')
 OR mode='disagreement' AND NOT coalesce(head.action IN ('correct','dispute'),false) THEN RETURN human_status(422); END IF;
 INSERT INTO human_review_event(workspace_id,run_id,version_id,request_key,actor,sequence,previous_id,
   action,outcome,reason,spans,selection,plan_id,machine,content_sha256,text_sha256)
 VALUES(w,r,v,k,who,coalesce(head.sequence,0)+1,expected,act,label::decision_outcome,note,evidence,mode,plan,d.outcome,d.content_sha256,d.text_sha256)
 RETURNING id INTO saved;
 RETURN human_status(201,saved);
END $$;

CREATE FUNCTION human_history(w uuid,src uuid,s uuid,r uuid,v uuid,skip int) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE d decision_row;
BEGIN
 IF skip IS NULL OR skip<0 OR NOT human_scope(w,src,s,r,false) THEN RETURN NULL; END IF;
 SELECT * INTO d FROM decision_row WHERE (workspace_id,run_id,version_id)=(w,r,v) AND outcome<>'excluded_blank';
 IF NOT FOUND THEN RETURN NULL; END IF;
 RETURN (SELECT jsonb_build_object(
   'head',(SELECT id FROM human_review_event WHERE (workspace_id,run_id,version_id)=(w,r,v) ORDER BY sequence DESC LIMIT 1),
   'total',(SELECT count(*) FROM human_review_event WHERE (workspace_id,run_id,version_id)=(w,r,v)),
   'machine',d.outcome,'content_sha256',d.content_sha256,'text_sha256',d.text_sha256,
   'events',coalesce(jsonb_agg(to_jsonb(page) ORDER BY sequence DESC),'[]'::jsonb))
 FROM (SELECT id,previous_id,sequence,actor,created_at,action,outcome,reason,spans,selection,plan_id
   FROM human_review_event WHERE (workspace_id,run_id,version_id)=(w,r,v) ORDER BY sequence DESC LIMIT 25 OFFSET skip) page);
END $$;

CREATE FUNCTION human_sample_ids(w uuid,r uuid,seed uuid,n int) RETURNS uuid[] LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT ARRAY(SELECT version_id FROM decision_row WHERE (workspace_id,run_id)=(w,r) AND outcome<>'excluded_blank'
 ORDER BY sha256(convert_to('human-sample-v1','UTF8')||uuid_send(w)||uuid_send(r)||uuid_send(seed)||uuid_send(version_id)),version_id LIMIT n)
$$;
CREATE FUNCTION human_sample_digest(w uuid,r uuid,seed uuid,n int,eligible int,ids uuid[]) RETURNS text LANGUAGE sql IMMUTABLE STRICT
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT encode(sha256(convert_to(jsonb_build_array('human-sample-v1',w,r,seed,n,eligible,ids)::text,'UTF8')),'hex')
$$;
CREATE FUNCTION human_plan(w uuid,src uuid,s uuid,r uuid,k uuid,seed_id uuid,n int) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE who uuid; prior human_review_plan; saved uuid; ids uuid[]; eligible int;
BEGIN
 IF NOT human_scope(w,src,s,r,true) THEN RETURN NULL; END IF;
 IF num_nulls(k,seed_id,n)>0 OR n NOT BETWEEN 1 AND 10000 THEN RETURN human_status(422); END IF;
 who:=access_actor();
 IF human_request_kind(w,k) IS NOT NULL THEN
   SELECT * INTO prior FROM human_review_plan WHERE (workspace_id,run_id,request_key)=(w,r,k);
   IF FOUND AND (prior.actor,prior.seed,prior.requested)=(who,seed_id,n) THEN RETURN human_status(200,prior.id); END IF;
   RETURN human_status(409);
 END IF;
 SELECT (manifest#>>'{summary,N_eligible}')::int INTO STRICT eligible FROM decision_binding WHERE (workspace_id,run_id)=(w,r);
 ids:=human_sample_ids(w,r,seed_id,n);
 INSERT INTO human_review_plan(workspace_id,run_id,request_key,actor,seed,requested,eligible,size,digest)
 VALUES(w,r,k,who,seed_id,n,eligible,cardinality(ids),human_sample_digest(w,r,seed_id,n,eligible,ids)) RETURNING id INTO saved;
 INSERT INTO human_plan_member SELECT w,r,saved,version_id,ord FROM unnest(ids) WITH ORDINALITY picked(version_id,ord);
 RETURN human_status(201,saved);
END $$;

CREATE FUNCTION human_plans(w uuid,src uuid,s uuid,r uuid,skip int) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF skip IS NULL OR skip<0 OR NOT human_scope(w,src,s,r,false) THEN RETURN NULL; END IF;
 RETURN (SELECT jsonb_build_object('total',(SELECT count(*) FROM human_review_plan WHERE (workspace_id,run_id)=(w,r)),
 'items',coalesce(jsonb_agg(to_jsonb(t)-'created_at' ORDER BY created_at DESC,id),'[]')) FROM
 (SELECT id,seed,requested,eligible,size,digest,created_at FROM human_review_plan WHERE (workspace_id,run_id)=(w,r)
 ORDER BY created_at DESC,id LIMIT 25 OFFSET skip) t);
END $$;

CREATE FUNCTION human_queue(w uuid,src uuid,s uuid,r uuid,mode text,label text,filter_state text,plan uuid,skip int)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE eligible int;
BEGIN
 IF num_nulls(mode,skip)>0 OR skip<0 OR mode NOT IN ('all','uncertain','disagreement','sample')
 OR label='excluded_blank' OR label<>ALL(enum_range(NULL::decision_outcome)::text[])
 OR filter_state NOT IN ('unreviewed','confirmed','corrected','disputed') OR (mode='sample')<>(plan IS NOT NULL)
 OR NOT human_scope(w,src,s,r,false) THEN RETURN NULL; END IF;
 IF plan IS NOT NULL AND NOT EXISTS(SELECT FROM human_review_plan WHERE (workspace_id,run_id,id)=(w,r,plan)) THEN RETURN NULL; END IF;
 SELECT (manifest#>>'{summary,N_eligible}')::int INTO STRICT eligible FROM decision_binding WHERE (workspace_id,run_id)=(w,r);
 -- Merge the three ordered populations without relying on fresh table cardinality estimates.
 -- A full-size sample with unknown statistics must never rescan the plan for every decision.
 RETURN (WITH heads AS (
    SELECT DISTINCT ON (version_id) version_id,machine,outcome,action,id,sequence FROM human_review_event
    WHERE (workspace_id,run_id)=(w,r) ORDER BY version_id DESC,sequence DESC
 ), streams AS (
    SELECT version_id,outcome machine,NULL::decision_outcome outcome,NULL::text action,NULL::uuid id,
      0::bigint sequence,NULL::int ordinal FROM decision_row WHERE (workspace_id,run_id)=(w,r) AND outcome<>'excluded_blank'
    UNION ALL SELECT *,NULL::int FROM heads
    UNION ALL SELECT version_id,NULL,NULL,NULL,NULL,-1,ordinal FROM human_plan_member
      WHERE (workspace_id,run_id,plan_id)=(w,r,plan)
 ), latest AS (
    SELECT DISTINCT ON (version_id) version_id,machine,outcome,action,id,
      max(ordinal) OVER (PARTITION BY version_id) ordinal FROM streams ORDER BY version_id,sequence DESC
 ), hits AS MATERIALIZED (
    SELECT version_id,machine,outcome,human_state(action) state,id event_id,
      CASE mode WHEN 'uncertain' THEN 'MACHINE_UNCERTAIN' WHEN 'sample' THEN 'FIXED_RANDOM_SAMPLE'
        WHEN 'disagreement' THEN CASE action WHEN 'dispute' THEN 'HUMAN_DISPUTE' ELSE 'HUMAN_MACHINE_DIFFERENCE' END
        ELSE 'ALL_ELIGIBLE' END reason,
      CASE WHEN mode='sample' THEN ordinal WHEN mode='disagreement' AND action='correct' THEN 1 ELSE 0 END priority
    FROM latest WHERE (label IS NULL OR machine=label::decision_outcome) AND (filter_state IS NULL OR human_state(action)=filter_state)
    AND CASE mode WHEN 'sample' THEN ordinal IS NOT NULL WHEN 'uncertain' THEN machine IN ('unclear','topic_uncertain')
         WHEN 'disagreement' THEN action IN ('correct','dispute') ELSE true END
 )
 SELECT jsonb_build_object('eligible',eligible,'total',(SELECT count(*) FROM hits),
 'rows',coalesce(jsonb_agg(to_jsonb(page)-'priority' ORDER BY priority,version_id),'[]'))
 FROM (SELECT * FROM hits ORDER BY priority,version_id LIMIT 25 OFFSET skip) page);
END $$;

-- Derive the publication receipt exclusively from the frozen member relation and original run.
-- Canonicalize timestamps to Unix seconds so the digest survives a restore in a different timezone.
CREATE FUNCTION human_result_document(w uuid,r uuid,revision uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 WITH baseline AS (
   SELECT b.manifest->>'manifest_sha256' manifest_sha256,b.manifest->>'rows_sha256' rows_sha256,
   b.manifest->>'digest' aspect_digest,a.checksum artifact_sha256,(b.manifest#>>'{summary,N_eligible}')::int eligible
   FROM decision_binding b JOIN analysis_run j ON (j.workspace_id,j.id)=(b.workspace_id,b.run_id)
   JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=(w,r,j.output_artifact_id)
   WHERE (b.workspace_id,b.run_id)=(w,r)
 ), counts AS (
   SELECT count(*) reviewed,count(*) FILTER(WHERE e.action<>'dispute') resolved,count(*) FILTER(WHERE e.action='confirm') confirmed,
     count(*) FILTER(WHERE e.action='correct') corrected,count(*) FILTER(WHERE e.action='dispute') disputed,
     count(*) FILTER(WHERE e.outcome IN ('complaint','mixed')) complaints,
     count(*) FILTER(WHERE e.outcome IN ('unclear','topic_uncertain')) uncertain,
     coalesce(string_agg(encode(sha256(convert_to(jsonb_build_array(e.version_id,e.id,e.sequence,e.previous_id,e.actor,
       extract(epoch FROM e.created_at),e.action,e.outcome,e.reason,e.spans,e.selection,e.plan_id,e.machine,
       e.content_sha256,e.text_sha256)::text,'UTF8')),'hex'),'' ORDER BY e.version_id),'') hashes
   FROM human_result_member m JOIN human_review_event e
   ON (e.workspace_id,e.run_id,e.version_id,e.id)=(m.workspace_id,m.run_id,m.version_id,m.event_id)
   WHERE (m.workspace_id,m.run_id,m.result_id)=(w,r,revision)
 ), report AS (
   SELECT *,jsonb_build_object('eligible',eligible,'reviewed',reviewed,'resolved',resolved,'confirmed',confirmed,'corrected',corrected,
     'disputed',disputed,'unreviewed',eligible-reviewed,'complaints',complaints,'uncertain',uncertain,
     'complaints_among_resolved',complaints::numeric/nullif(resolved,0)) summary FROM counts,baseline
 )
 SELECT jsonb_build_object('summary',summary,'manifest_sha256',manifest_sha256,'artifact_sha256',artifact_sha256,
 'rows_sha256',rows_sha256,'digest',encode(sha256(convert_to(jsonb_build_array('human-resolved-v1',w,r,aspect_digest,
 manifest_sha256,rows_sha256,artifact_sha256,summary,hashes)::text,'UTF8')),'hex')) FROM report
$$;

CREATE FUNCTION human_publish(w uuid,src uuid,s uuid,r uuid,k uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE who uuid; prior human_review_result; saved uuid:=gen_random_uuid(); doc jsonb;
BEGIN
 IF NOT human_scope(w,src,s,r,true) THEN RETURN NULL; END IF;
 IF k IS NULL THEN RETURN human_status(422); END IF;
 who:=access_actor();
 IF human_request_kind(w,k) IS NOT NULL THEN
   SELECT * INTO prior FROM human_review_result WHERE (workspace_id,run_id,request_key)=(w,r,k);
   IF FOUND AND prior.actor=who THEN RETURN human_status(200,prior.id); END IF;
   RETURN human_status(409);
 END IF;
 -- The parent FK is deferred; an error rolls back membership and publication together.
 INSERT INTO human_result_member SELECT DISTINCT ON (version_id) w,r,saved,version_id,id FROM human_review_event
 WHERE (workspace_id,run_id)=(w,r) ORDER BY version_id DESC,sequence DESC;
 doc:=human_result_document(w,r,saved);
 INSERT INTO human_review_result(workspace_id,run_id,id,request_key,actor,digest,manifest_sha256,rows_sha256,artifact_sha256,summary)
 VALUES(w,r,saved,k,who,doc->>'digest',doc->>'manifest_sha256',doc->>'rows_sha256',doc->>'artifact_sha256',doc->'summary');
 RETURN human_status(201,saved);
END $$;
CREATE FUNCTION human_results(w uuid,src uuid,s uuid,r uuid,skip int) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF skip IS NULL OR skip<0 OR NOT human_scope(w,src,s,r,false) THEN RETURN NULL; END IF;
 RETURN (SELECT jsonb_build_object('total',(SELECT count(*) FROM human_review_result WHERE (workspace_id,run_id)=(w,r)),
 'items',coalesce(jsonb_agg(to_jsonb(t) ORDER BY created_at DESC,id),'[]')) FROM
 (SELECT id,created_at FROM human_review_result WHERE (workspace_id,run_id)=(w,r) ORDER BY created_at DESC,id LIMIT 25 OFFSET skip) t);
END $$;
CREATE FUNCTION human_result(w uuid,src uuid,s uuid,r uuid,revision uuid,skip int) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE saved human_review_result;
BEGIN
 IF skip IS NULL OR skip<0 OR NOT human_scope(w,src,s,r,false) THEN RETURN NULL; END IF;
 SELECT * INTO saved FROM human_review_result WHERE (workspace_id,run_id,id)=(w,r,revision);
 IF NOT FOUND THEN RETURN NULL; END IF;
 RETURN (SELECT jsonb_build_object('id',saved.id,'created_at',saved.created_at,'actor',saved.actor,'digest',saved.digest,
   'manifest_sha256',saved.manifest_sha256,'rows_sha256',saved.rows_sha256,'artifact_sha256',saved.artifact_sha256,
   'summary',saved.summary,'total',(saved.summary->>'eligible')::int,'rows',coalesce(jsonb_agg(to_jsonb(t) ORDER BY version_id),'[]'))
 FROM (WITH page AS MATERIALIZED (SELECT version_id,outcome FROM decision_row WHERE (workspace_id,run_id)=(w,r)
    AND outcome<>'excluded_blank' ORDER BY version_id LIMIT 25 OFFSET skip)
   SELECT d.version_id,d.outcome machine,m.event_id,e.outcome,human_state(e.action) state
   FROM page d LEFT JOIN human_result_member m ON (m.workspace_id,m.run_id,m.result_id,m.version_id)=(w,r,revision,d.version_id)
   LEFT JOIN human_review_event e ON (e.workspace_id,e.run_id,e.version_id,e.id)=(w,r,d.version_id,m.event_id)) t);
END $$;

CREATE FUNCTION human_export_runs() RETURNS TABLE(w uuid,r uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT b.workspace_id,b.run_id FROM decision_binding b
 JOIN snapshot sn ON (sn.workspace_id,sn.source_id,sn.id)=(b.workspace_id,b.source_id,b.snapshot_id)
 JOIN import_batch i ON (i.workspace_id,i.source_id,i.id)=(b.workspace_id,b.source_id,sn.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(b.workspace_id,b.source_id,i.file_sha256)
 WHERE session_user='rs_retention' AND a.human_review_allowed AND b.manifest IS NOT NULL AND decision_exportable(b.workspace_id,b.run_id)
$$;

CREATE FUNCTION human_integrity(w uuid,r uuid) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF session_user='rs_retention' AND (w,r) NOT IN (SELECT * FROM human_export_runs()) THEN RETURN false; END IF;
 RETURN NOT EXISTS(WITH chain AS (SELECT e.*,row_number() OVER history seq,lag(e.id) OVER history prev
    FROM human_review_event e WHERE (workspace_id,run_id)=(w,r) WINDOW history AS (PARTITION BY version_id ORDER BY sequence))
   SELECT FROM chain e JOIN decision_row d ON (d.workspace_id,d.run_id,d.version_id)=(w,r,e.version_id)
   JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(w,d.source_id,d.version_id)
   WHERE e.seq<>e.sequence OR e.prev IS DISTINCT FROM e.previous_id OR
   (d.outcome,d.content_sha256,d.text_sha256)<>(e.machine,e.content_sha256,e.text_sha256)
   OR NOT human_spans_valid(e.spans,v.text_content) OR NOT aspect_nonblank(v.text_content)
   OR e.text_sha256<>encode(sha256(convert_to(v.text_content,'UTF8')),'hex'))
 AND NOT EXISTS(SELECT FROM human_review_plan p CROSS JOIN LATERAL (
   SELECT coalesce(array_agg(version_id ORDER BY ordinal),'{}'::uuid[]) ids,
   coalesce(max(ordinal),0) last_ordinal FROM human_plan_member WHERE (workspace_id,run_id,plan_id)=(w,r,p.id)
 ) m WHERE (p.workspace_id,p.run_id)=(w,r) AND
   (p.eligible<>(SELECT (manifest#>>'{summary,N_eligible}')::int FROM decision_binding WHERE (workspace_id,run_id)=(w,r))
    OR p.size<>cardinality(m.ids) OR p.size<>m.last_ordinal OR m.ids<>human_sample_ids(w,r,p.seed,p.requested)
    OR p.digest<>human_sample_digest(w,r,p.seed,p.requested,p.eligible,m.ids)))
 AND NOT EXISTS(SELECT FROM human_review_result res WHERE (res.workspace_id,res.run_id)=(w,r)
   AND jsonb_build_object('digest',res.digest,'manifest_sha256',res.manifest_sha256,'artifact_sha256',res.artifact_sha256,
   'rows_sha256',res.rows_sha256,'summary',res.summary) IS DISTINCT FROM human_result_document(w,r,res.id));
END $$;

ALTER FUNCTION retention_plan(uuid,text,uuid) RENAME TO pre_human_retention_plan;
CREATE FUNCTION retention_plan(w uuid,k text,t uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 WITH result AS (SELECT pre_human_retention_plan(w,k,t) doc),
 runs AS MATERIALIZED (SELECT run_id FROM decision_binding WHERE workspace_id=w AND source_id IN (SELECT retention_sources(w,k,t)))
 SELECT jsonb_set(doc,'{tables}',(doc->'tables')||jsonb_build_object(
 'human_review_event',(SELECT count(*) FROM human_review_event WHERE workspace_id=w AND run_id IN (SELECT run_id FROM runs)),
 'human_review_plan',(SELECT count(*) FROM human_review_plan WHERE workspace_id=w AND run_id IN (SELECT run_id FROM runs)),
 'human_plan_member',(SELECT count(*) FROM human_plan_member WHERE workspace_id=w AND run_id IN (SELECT run_id FROM runs)),
 'human_review_result',(SELECT count(*) FROM human_review_result WHERE workspace_id=w AND run_id IN (SELECT run_id FROM runs)),
 'human_result_member',(SELECT count(*) FROM human_result_member WHERE workspace_id=w AND run_id IN (SELECT run_id FROM runs)))) FROM result
$$;

DO $$
DECLARE tab text;
BEGIN
 FOREACH tab IN ARRAY ARRAY['human_review_event','human_review_plan','human_plan_member','human_review_result','human_result_member'] LOOP
   EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tab);
   EXECUTE format('REVOKE ALL ON %I FROM PUBLIC,rs_runtime,rs_worker,rs_upload,rs_retention',tab);
   EXECUTE format('CREATE POLICY owner_export ON %I FOR SELECT TO rs_retention USING ((workspace_id,run_id) IN (SELECT * FROM human_export_runs()))',tab);
   EXECUTE format('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_history_change()',tab);
   EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION reject_history_change()',tab);
 END LOOP;
END $$;
GRANT SELECT(workspace_id,run_id,id,actor,created_at,seed,requested,eligible,size,algorithm,digest) ON human_review_plan TO rs_retention;
GRANT SELECT(workspace_id,run_id,plan_id,version_id,ordinal) ON human_plan_member TO rs_retention;
GRANT SELECT(workspace_id,run_id,version_id,id,sequence,previous_id,actor,created_at,action,outcome,reason,spans,selection,plan_id,machine,content_sha256,text_sha256) ON human_review_event TO rs_retention;
GRANT SELECT(workspace_id,run_id,id,actor,created_at,algorithm,digest,manifest_sha256,rows_sha256,artifact_sha256,summary) ON human_review_result TO rs_retention;
GRANT SELECT(workspace_id,run_id,result_id,version_id,event_id) ON human_result_member TO rs_retention;

ALTER FUNCTION browser_role_safe() RENAME TO pre_human_browser_role_safe;
REVOKE EXECUTE ON FUNCTION pre_human_browser_role_safe() FROM rs_upload;
CREATE FUNCTION browser_role_safe() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT pre_human_browser_role_safe() AND (SELECT count(*)=5 AND bool_and(c.relrowsecurity AND
 NOT has_any_column_privilege('rs_upload',c.oid,'SELECT,INSERT,UPDATE,REFERENCES')) FROM pg_class c
 WHERE c.relnamespace='public'::regnamespace AND c.relname IN ('human_review_event','human_review_plan','human_plan_member','human_review_result','human_result_member'))
$$;

REVOKE ALL ON FUNCTION browser_role_safe(),retention_plan(uuid,text,uuid),human_export_runs(),human_integrity(uuid,uuid),
 human_scope(uuid,uuid,uuid,uuid,boolean),human_state(text),human_status(integer,uuid),human_request_kind(uuid,uuid),
 human_spans_valid(jsonb,text),human_sample_ids(uuid,uuid,uuid,int),human_sample_digest(uuid,uuid,uuid,int,int,uuid[]),
 human_result_document(uuid,uuid,uuid),human_append(uuid,uuid,uuid,uuid,uuid,uuid,uuid,text,text,text,jsonb,text,uuid),
 human_history(uuid,uuid,uuid,uuid,uuid,int),human_queue(uuid,uuid,uuid,uuid,text,text,text,uuid,int),
 human_plan(uuid,uuid,uuid,uuid,uuid,uuid,int),human_plans(uuid,uuid,uuid,uuid,int),human_publish(uuid,uuid,uuid,uuid,uuid),
 human_results(uuid,uuid,uuid,uuid,int),human_result(uuid,uuid,uuid,uuid,uuid,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_role_safe() TO rs_upload;
GRANT EXECUTE ON FUNCTION human_export_runs(),human_integrity(uuid,uuid) TO rs_retention;
GRANT EXECUTE ON FUNCTION human_append(uuid,uuid,uuid,uuid,uuid,uuid,uuid,text,text,text,jsonb,text,uuid),
 human_history(uuid,uuid,uuid,uuid,uuid,int),human_queue(uuid,uuid,uuid,uuid,text,text,text,uuid,int),
 human_plan(uuid,uuid,uuid,uuid,uuid,uuid,int),human_plans(uuid,uuid,uuid,uuid,int),human_publish(uuid,uuid,uuid,uuid,uuid),
 human_results(uuid,uuid,uuid,uuid,int),human_result(uuid,uuid,uuid,uuid,uuid,int) TO rs_runtime;
