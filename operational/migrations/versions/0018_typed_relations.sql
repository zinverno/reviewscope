-- Every permission is specific to the admitted bytes. Preserve the 0017 business seal when off.
ALTER TABLE browser_admission ADD COLUMN relation_allowed boolean NOT NULL DEFAULT false;
CREATE OR REPLACE FUNCTION retention_access_seal() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT CASE WHEN count(*)=0 THEN retention_access_seal_v6() ELSE encode(sha256(convert_to(retention_access_seal_v6() || string_agg(v,'' ORDER BY v),'UTF8')),'hex') END
 FROM (SELECT (to_jsonb(a) - CASE WHEN a.decision_allowed THEN ARRAY[]::text[] ELSE ARRAY['decision_allowed'] END
                       - CASE WHEN a.relation_allowed THEN ARRAY[]::text[] ELSE ARRAY['relation_allowed'] END
                       - CASE WHEN a.human_review_allowed THEN ARRAY[]::text[] ELSE ARRAY['human_review_allowed'] END)::text v
 FROM browser_admission a UNION ALL
 SELECT jsonb_build_array(sl.workspace_id,sl.source_id,sl.project_id,sl.location_id,sl.external_place_id,
 s.kind,s.source_key,s.external_source_id)::text FROM source_location sl JOIN source s
 ON (s.workspace_id,s.id)=(sl.workspace_id,sl.source_id)
 WHERE EXISTS(SELECT FROM browser_admission a WHERE (a.workspace_id,a.source_id)=(s.workspace_id,s.id))) grants
$$;

CREATE TABLE relation_result (
 workspace_id uuid NOT NULL, source_id uuid NOT NULL, snapshot_id uuid NOT NULL,
 id uuid NOT NULL, request_key uuid NOT NULL, actor uuid NOT NULL REFERENCES principal,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), run_id uuid, revision_id uuid, manifest jsonb NOT NULL, digest sha256_hex NOT NULL,
 PRIMARY KEY(workspace_id,snapshot_id,id), UNIQUE(workspace_id,request_key),
 CHECK ((run_id IS NULL)=(revision_id IS NULL)), CHECK(octet_length(manifest::text)<=65536),
 FOREIGN KEY(workspace_id,source_id,snapshot_id) REFERENCES snapshot(workspace_id,source_id,id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,run_id,snapshot_id,source_id) REFERENCES decision_binding(workspace_id,run_id,snapshot_id,source_id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,run_id,revision_id) REFERENCES human_review_result ON DELETE CASCADE
);
CREATE INDEX relation_result_source ON relation_result(workspace_id,source_id);
CREATE TABLE relation_pair (
 workspace_id uuid NOT NULL, snapshot_id uuid NOT NULL, result_id uuid NOT NULL,
 left_id uuid NOT NULL, right_id uuid NOT NULL CHECK(left_id<right_id), evidence jsonb NOT NULL,
 PRIMARY KEY(workspace_id,snapshot_id,result_id,left_id,right_id),
 CHECK ((evidence#>>'{left,version_id}',evidence#>>'{right,version_id}') IS NOT DISTINCT FROM (left_id::text,right_id::text)),
 FOREIGN KEY(workspace_id,snapshot_id,result_id) REFERENCES relation_result ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(workspace_id,snapshot_id,left_id) REFERENCES snapshot_member ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,snapshot_id,right_id) REFERENCES snapshot_member ON DELETE CASCADE
);
CREATE INDEX relation_pair_left ON relation_pair(workspace_id,snapshot_id,left_id);
CREATE INDEX relation_pair_right ON relation_pair(workspace_id,snapshot_id,right_id);

CREATE FUNCTION relation_admitted(w uuid,src uuid,s uuid) RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT candidate_admitted(w,s) AND EXISTS(SELECT FROM snapshot sn
 JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,src,b.file_sha256)
 WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s) AND a.relation_allowed
 AND b.status='accepted' AND b.delivery IS NOT NULL)
$$;
CREATE FUNCTION relation_scope(w uuid,src uuid,s uuid,r uuid,h uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF session_user<>'rs_runtime' OR (r IS NULL)<>(h IS NULL) OR retention_online() IS NOT TRUE
 OR candidate_scope(w,src,s) IS NOT TRUE OR relation_admitted(w,src,s) IS NOT TRUE THEN RETURN false; END IF;
 IF r IS NOT NULL AND (human_scope(w,src,s,r,false) IS NOT TRUE OR NOT EXISTS(SELECT FROM human_review_result
    WHERE (workspace_id,run_id,id)=(w,r,h)) OR NOT EXISTS(SELECT FROM decision_binding
    WHERE (workspace_id,source_id,snapshot_id,run_id)=(w,src,s,r) AND digest IN (
    'd4614d09b445e4a27bb2fd7690646ec6cfbe3aceddd3968e9c2d406710ad6848', -- queue before ordering
    '7f3c7010e46abc7e3e309feb0618f71c954e6eaab6b8904e5f299bc66e04ea48'  -- handover after readiness
    ))) THEN RETURN false; END IF;
 RETURN EXISTS(SELECT FROM access_session WHERE token_hash=sha256(convert_to(current_setting('rs.session_proof',true),'UTF8'))
 AND revoked_at IS NULL AND expires_at>clock_timestamp());
END $$;

-- Only the ticket broker resolves an object location. It returns a receipt, never the snapshot bytes.
CREATE FUNCTION relation_object(w uuid,src uuid,s uuid,r uuid,actor uuid,expires bigint,ticket text)
RETURNS TABLE(root text,digest text,bytes bigint,manifest text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT ws.delivery_object_root,sn.duckdb_sha256::text,4194304::bigint,sn.manifest_sha256::text
 FROM workspace ws JOIN snapshot sn ON (sn.workspace_id,sn.source_id,sn.id)=(w,src,s)
 JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(w,actor) JOIN principal p ON p.id=actor
 WHERE session_user='rs_upload' AND r=s AND ws.id=w AND ws.state='active' AND retention_online()
 AND m.state='active' AND m.role IN ('owner','analyst') AND p.state='active' AND relation_admitted(w,src,s)
 AND expires BETWEEN floor(extract(epoch FROM statement_timestamp()))::bigint AND floor(extract(epoch FROM statement_timestamp()))::bigint+30
 AND EXISTS(SELECT FROM access_session x WHERE x.principal_id=actor AND x.revoked_at IS NULL AND x.expires_at>statement_timestamp()
 AND encode(sha256(x.token_hash || sha256(convert_to(concat_ws('|','relation-snapshot-1',w,src,s,r,actor,expires),'UTF8'))),'hex')=ticket)
$$;

CREATE FUNCTION relation_endpoints(w uuid,src uuid,s uuid,r uuid,h uuid)
RETURNS TABLE(id uuid,body text,norm text,words text[],endpoint jsonb) LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT v.id,v.text_content,n.val,regexp_split_to_array(n.val,' +'),jsonb_build_object(
 'version_id',v.id,'content_sha256',v.content_sha256,'text_sha256',encode(sha256(convert_to(v.text_content,'UTF8')),'hex'),
 'normalized_sha256',encode(sha256(convert_to(n.val,'UTF8')),'hex'),'event_id',e.id,'state',human_state(e.action),
 'stance',e.outcome,'spans',coalesce(e.spans,'[]'))
 FROM snapshot_member sm JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(w,src,sm.version_id)
 CROSS JOIN LATERAL (SELECT btrim(regexp_replace(lower(v.text_content COLLATE "und-x-icu"),'[[:space:]]+',' ','g')) val) n
 LEFT JOIN human_result_member m ON (m.workspace_id,m.run_id,m.result_id,m.version_id)=(w,r,h,v.id)
 LEFT JOIN human_review_event e ON (e.workspace_id,e.run_id,e.version_id,e.id)=(w,r,v.id,m.event_id)
 WHERE (sm.workspace_id,sm.source_id,sm.snapshot_id)=(w,src,s) AND aspect_nonblank(v.text_content)
$$;

CREATE FUNCTION relation_projection(w uuid,src uuid,s uuid,r uuid,h uuid)
RETURNS TABLE(left_id uuid,right_id uuid,evidence jsonb) LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 WITH docs AS MATERIALIZED (SELECT e.* FROM snapshot sn CROSS JOIN LATERAL relation_endpoints(w,src,s,r,h) e
 WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s) AND cardinality(sn.version_ids)<=32), matches AS (
 SELECT a.id l,b.id rr,a.endpoint x,b.endpoint y,length(a.body) la,length(b.body) lb,a.norm=b.norm eq,a.body=b.body raw_equal,
 shared.overlap,shared.witness, strpos(a.body,shared.witness)-1 lo_a,strpos(b.body,shared.witness)-1 lo_b,
 a.endpoint->>'event_id' IS NOT NULL AND b.endpoint->>'event_id' IS NOT NULL reviewed
 FROM docs a JOIN docs b ON a.id<b.id
 CROSS JOIN LATERAL (SELECT count(DISTINCT word) overlap,min(word COLLATE "C") witness
 FROM unnest(a.words) word WHERE length(word)>=4 AND word=ANY(b.words)) shared
 ), labels AS (SELECT *, CASE WHEN h IS NULL THEN 'not_evaluated'
 WHEN NOT reviewed OR x->>'state'='disputed' OR y->>'state'='disputed' THEN 'uncertain'
 WHEN x->>'stance' IN ('mixed','unclear','topic_uncertain') OR y->>'stance' IN ('mixed','unclear','topic_uncertain') THEN 'uncertain'
 WHEN (x->>'stance' IN ('denial','complaint','positive') AND x->'spans'='[]') OR (y->>'stance' IN ('denial','complaint','positive') AND y->'spans'='[]') THEN 'uncertain'
 WHEN x->>'stance'='complaint' AND y->>'stance'='complaint' THEN 'same_specific_complaint'
 WHEN ARRAY[x->>'stance',y->>'stance'] @> ARRAY['complaint','denial'] THEN 'opposite_stance'
 ELSE 'no_relation' END kind FROM matches WHERE eq OR overlap>0 OR reviewed)
 SELECT l,rr,jsonb_build_object('left',x,'right',y,'text_spans',CASE WHEN eq THEN jsonb_build_array(jsonb_build_array(0,la),jsonb_build_array(0,lb))
 WHEN lo_a>=0 AND lo_b>=0 THEN jsonb_build_array(jsonb_build_array(lo_a,lo_a+length(witness)),jsonb_build_array(lo_b,lo_b+length(witness))) END,'channels',jsonb_strip_nulls(jsonb_build_object(
 'exact',CASE WHEN eq THEN CASE WHEN raw_equal THEN 'raw_utf8' ELSE 'icu_lower_whitespace' END END,
 'shared_words',nullif(overlap,0),'frozen_endpoints',CASE WHEN reviewed THEN true END)),
 'text_kind',CASE WHEN eq THEN 'exact_text' WHEN overlap>0 THEN 'text_similarity_candidate' ELSE 'not_evaluated' END,
 'text_basis',CASE WHEN eq THEN 'deterministic_text' WHEN overlap>0 THEN 'machine_experimental' ELSE 'not_evaluated' END,
 'aspect_kind',kind,'aspect_basis',CASE WHEN kind IN ('same_specific_complaint','opposite_stance','no_relation')
 THEN 'derived_from_human_outcomes' ELSE kind END,
 'reason',CASE WHEN h IS NULL THEN 'no_revision' WHEN NOT reviewed THEN 'unreviewed_endpoint'
 WHEN kind='uncertain' THEN 'disputed_ambiguous_or_missing_citation' ELSE 'frozen_endpoint_stances' END)
 FROM labels
$$;

CREATE FUNCTION relation_manifest(w uuid,src uuid,s uuid,r uuid,h uuid,res uuid) RETURNS jsonb LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 WITH stats AS (SELECT count(*) n,coalesce(jsonb_agg(encode(sha256(convert_to(evidence::text,'UTF8')),'hex') ORDER BY left_id,right_id),'[]') row_hashes,coalesce(string_agg(encode(sha256(convert_to(evidence::text,'UTF8')),'hex'),'' ORDER BY left_id,right_id),'') hashes
 FROM relation_pair WHERE (workspace_id,snapshot_id,result_id)=(w,s,res)),
 eligible AS (SELECT count(*) n FROM relation_endpoints(w,src,s,r,h))
 SELECT jsonb_build_object('profile','direct-pairs-small-v1','method_sha256',encode(sha256(convert_to(
 pg_get_functiondef('relation_projection(uuid,uuid,uuid,uuid,uuid)'::regprocedure)||pg_get_functiondef('relation_endpoints(uuid,uuid,uuid,uuid,uuid)'::regprocedure),'UTF8')),'hex'),
 'channel_availability',jsonb_build_object('normalized_equality','available','shared_words','available','dense','not_evaluated',
 'human_revision',CASE WHEN h IS NULL THEN 'not_evaluated' ELSE 'available' END),'source_id',src,'project_id',z.project_id,'snapshot_id',s,
 'workspace_id',w,'snapshot_digest',sn.manifest_sha256,'snapshot_object',sn.duckdb_sha256,
 'decision_run',r,'human_revision',h,'human_digest',hr.digest,'aspect_id',d.aspect_id,'aspect_version_id',d.version_id,'definition_digest',d.digest,
 'eligible_review_count',e.n,'excluded_blank_count',cardinality(sn.version_ids)-e.n,'total_possible_pairs',e.n*(e.n-1)/2,
 'exact_possible_pairs',(SELECT count(*) FROM relation_pair WHERE (workspace_id,snapshot_id,result_id)=(w,s,res) AND evidence->>'text_kind'='exact_text'),
 'candidate_pairs_count',a.n,'evaluated_pairs_count',a.n,'published_edges_count',a.n,'budget',496,'dense','not_evaluated',
 'kind_counts',(SELECT coalesce(jsonb_object_agg(axis,counts),'{}') FROM (
 SELECT axis,jsonb_object_agg(kind,n) counts FROM (SELECT axis,evidence->>axis kind,count(*) n FROM relation_pair
 CROSS JOIN unnest(ARRAY['text_kind','aspect_kind']) axis WHERE (workspace_id,snapshot_id,result_id)=(w,s,res) GROUP BY 1,2) x GROUP BY axis) y),
 'row_hashes',a.row_hashes,'rows_sha256',encode(sha256(convert_to(a.hashes,'UTF8')),'hex'))
 FROM snapshot sn JOIN source z ON (z.workspace_id,z.id)=(w,src) CROSS JOIN stats a CROSS JOIN eligible e
 LEFT JOIN decision_binding d ON (d.workspace_id,d.run_id)=(w,r)
 LEFT JOIN human_review_result hr ON (hr.workspace_id,hr.run_id,hr.id)=(w,r,h)
 WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s)
$$;

CREATE FUNCTION relation_publish(w uuid,src uuid,s uuid,r uuid,h uuid,k uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE sn snapshot; prior relation_result; saved uuid:=gen_random_uuid(); doc jsonb; expected_count int; expected_sha text;
BEGIN
 IF k IS NULL OR NOT relation_scope(w,src,s,r,h) THEN RETURN NULL; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(w::text,2418));
 SELECT * INTO prior FROM relation_result WHERE (workspace_id,request_key)=(w,k);
 IF FOUND THEN
   IF (prior.source_id,prior.snapshot_id,prior.run_id,prior.revision_id,prior.actor) IS DISTINCT FROM (src,s,r,h,access_actor()) THEN RETURN human_status(409); END IF;
   RETURN human_status(200,prior.id);
 END IF;
 IF (SELECT count(*) FROM relation_result WHERE workspace_id=w AND created_at>clock_timestamp()-interval '1 hour')>=24 THEN RETURN human_status(429); END IF;
 SELECT * INTO STRICT sn FROM snapshot WHERE (workspace_id,source_id,id)=(w,src,s);
 IF cardinality(sn.version_ids)>32 THEN RETURN human_status(413); END IF;
 IF EXISTS(SELECT FROM review_version WHERE workspace_id=w AND source_id=src AND id=ANY(sn.version_ids) AND octet_length(text_content)>4096)
 THEN RETURN human_status(413); END IF;
 IF sn.version_ids IS DISTINCT FROM ARRAY(SELECT sm.version_id FROM snapshot_member sm WHERE (sm.workspace_id,sm.source_id,sm.snapshot_id)=(w,src,s) ORDER BY sm.version_id)
 OR sn.version_ids IS DISTINCT FROM ARRAY(SELECT ir.version_id FROM import_row ir WHERE (ir.workspace_id,ir.source_id,ir.batch_id)=(w,src,sn.batch_id) AND status='accepted' ORDER BY ir.version_id)
 THEN RETURN human_status(503); END IF;
 -- Hash the proposed set before storage. The manifest below hashes the actual persisted rows.
 WITH proposed AS MATERIALIZED (SELECT * FROM relation_projection(w,src,s,r,h)),
 written AS (INSERT INTO relation_pair SELECT w,s,saved,p.* FROM proposed p)
 SELECT count(*),encode(sha256(convert_to(coalesce(string_agg(encode(sha256(convert_to(evidence::text,'UTF8')),'hex'),
 '' ORDER BY left_id,right_id),''),'UTF8')),'hex') INTO expected_count,expected_sha FROM proposed;
 doc:=relation_manifest(w,src,s,r,h,saved);
 IF (expected_count,expected_sha) IS DISTINCT FROM ((doc->>'candidate_pairs_count')::int,doc->>'rows_sha256') THEN
   RAISE EXCEPTION 'incomplete_relation_publication';
 END IF;
 IF NOT relation_scope(w,src,s,r,h) THEN RAISE EXCEPTION 'scope_closed'; END IF;
 INSERT INTO relation_result(workspace_id,source_id,snapshot_id,id,request_key,actor,run_id,revision_id,manifest,digest) VALUES(w,src,s,saved,k,access_actor(),r,h,doc,encode(sha256(convert_to(doc::text,'UTF8')),'hex'));
 RETURN human_status(201,saved);
END $$;

CREATE FUNCTION relation_read(w uuid,src uuid,s uuid,res uuid,skip int,l uuid,rr uuid,exact_only boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE saved relation_result; pairs jsonb; filtered int;
BEGIN
 IF skip IS NULL OR skip NOT BETWEEN 0 AND 496 OR (l IS NULL)<>(rr IS NULL) OR l>=rr THEN RETURN NULL; END IF;
 SELECT * INTO saved FROM relation_result WHERE (workspace_id,source_id,snapshot_id,id)=(w,src,s,res);
 IF NOT FOUND OR NOT relation_scope(w,src,s,saved.run_id,saved.revision_id) THEN RETURN NULL; END IF;
 IF saved.digest<>encode(sha256(convert_to(saved.manifest::text,'UTF8')),'hex') THEN RAISE EXCEPTION 'invalid_manifest'; END IF;
 filtered:=CASE WHEN exact_only THEN coalesce((saved.manifest#>>'{kind_counts,text_kind,exact_text}')::int,0)
 ELSE (saved.manifest->>'candidate_pairs_count')::int END;
 SELECT coalesce(jsonb_agg(evidence ORDER BY left_id,right_id),'[]') INTO pairs FROM
 (SELECT left_id,right_id,evidence FROM relation_pair WHERE (workspace_id,snapshot_id,result_id)=(w,s,res)
 AND (NOT exact_only OR evidence->>'text_kind'='exact_text') AND (l IS NULL OR (left_id,right_id)=(l,rr)) ORDER BY left_id,right_id OFFSET skip LIMIT 10) p;
 IF EXISTS(SELECT FROM jsonb_array_elements(pairs) e WHERE NOT (saved.manifest->'row_hashes' ? encode(sha256(convert_to(e::text,'UTF8')),'hex')))
 OR (l IS NULL AND jsonb_array_length(pairs)<>least(10,greatest(0,filtered-skip)))
 THEN RAISE EXCEPTION 'invalid_pair_page'; END IF;
 IF l IS NOT NULL THEN
   IF pairs='[]' THEN RETURN NULL; END IF;
   IF NOT EXISTS(SELECT FROM review_version a JOIN review_version b ON (b.workspace_id,b.source_id,b.id)=(w,src,rr)
      WHERE (a.workspace_id,a.source_id,a.id)=(w,src,l) AND
      (a.id::text,a.content_sha256,b.id::text,b.content_sha256)=(pairs#>>'{0,left,version_id}',pairs#>>'{0,left,content_sha256}',
      pairs#>>'{0,right,version_id}',pairs#>>'{0,right,content_sha256}')) THEN RAISE EXCEPTION 'invalid_endpoints'; END IF;
 END IF;
 RETURN jsonb_build_object('id',res,'digest',saved.digest,'manifest',saved.manifest-'row_hashes','filtered',filtered,'pairs',pairs);
END $$;
CREATE FUNCTION relation_export_ids() RETURNS TABLE(w uuid,s uuid,id uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT p.workspace_id,p.snapshot_id,p.id FROM relation_result p
 WHERE session_user='rs_retention' AND (p.workspace_id,p.source_id) IN (SELECT * FROM retention_export_sources())
 AND relation_admitted(p.workspace_id,p.source_id,p.snapshot_id) AND
 (p.run_id IS NULL OR (p.workspace_id,p.run_id) IN (SELECT * FROM human_export_runs()))
$$;
CREATE FUNCTION relation_intact(w uuid,s uuid,res uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT p.digest=encode(sha256(convert_to(p.manifest::text,'UTF8')),'hex')
 AND p.manifest=relation_manifest(w,p.source_id,s,p.run_id,p.revision_id,res)
 AND NOT EXISTS(SELECT left_id,right_id,evidence FROM relation_projection(w,p.source_id,s,p.run_id,p.revision_id)
 EXCEPT SELECT left_id,right_id,evidence FROM relation_pair WHERE (workspace_id,snapshot_id,result_id)=(w,s,res))
 AND (SELECT count(*) FROM relation_projection(w,p.source_id,s,p.run_id,p.revision_id))=(p.manifest->>'candidate_pairs_count')::int
 FROM relation_result p WHERE (p.workspace_id,p.snapshot_id,p.id)=(w,s,res)
 AND (session_user<>'rs_retention' OR (w,s,res) IN (SELECT * FROM relation_export_ids()))
$$;
ALTER FUNCTION retention_plan(uuid,text,uuid) RENAME TO pre_relation_retention_plan;
CREATE FUNCTION retention_plan(w uuid,k text,t uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 WITH base AS (SELECT pre_relation_retention_plan(w,k,t) d), results AS MATERIALIZED
 (SELECT snapshot_id,id,octet_length(manifest::text) manifest_bytes FROM relation_result WHERE workspace_id=w AND source_id IN (SELECT retention_sources(w,k,t))),
 pairs AS (SELECT count(*) n,coalesce(sum(octet_length(evidence::text)),0) bytes FROM relation_pair WHERE workspace_id=w AND (snapshot_id,result_id) IN (SELECT snapshot_id,id FROM results))
 SELECT jsonb_set(d,'{tables}',(d->'tables')||jsonb_build_object('relation_result',(SELECT count(*) FROM results),
 'relation_pair',(SELECT n FROM pairs))) || jsonb_build_object('relation_payload_bytes',
 (SELECT coalesce(sum(manifest_bytes),0) FROM results)+(SELECT bytes FROM pairs)) FROM base
$$;
DO $$
DECLARE tab text;
BEGIN
 FOREACH tab IN ARRAY ARRAY['relation_result','relation_pair'] LOOP
   EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tab);
   EXECUTE format('REVOKE ALL ON %I FROM PUBLIC,rs_runtime,rs_worker,rs_upload,rs_retention',tab);
   EXECUTE format('CREATE POLICY owner_export ON %I FOR SELECT TO rs_retention USING ((workspace_id,snapshot_id,%I) IN (SELECT * FROM relation_export_ids()))',tab,CASE tab WHEN 'relation_pair' THEN 'result_id' ELSE 'id' END);
   EXECUTE format('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_history_change()',tab);
   EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION reject_history_change()',tab);
 END LOOP;
END $$;
ALTER FUNCTION browser_role_safe() RENAME TO pre_relation_browser_role_safe;
REVOKE EXECUTE ON FUNCTION pre_relation_browser_role_safe() FROM rs_upload;
CREATE FUNCTION browser_role_safe() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT pre_relation_browser_role_safe() AND (SELECT count(*)=2 AND bool_and(c.relrowsecurity AND
 NOT has_any_column_privilege('rs_upload',c.oid,'SELECT,INSERT,UPDATE,REFERENCES')) FROM pg_class c
 WHERE c.relnamespace='public'::regnamespace AND c.relname IN ('relation_pair','relation_result'))
$$;
REVOKE ALL ON FUNCTION relation_object(uuid,uuid,uuid,uuid,uuid,bigint,text),relation_scope(uuid,uuid,uuid,uuid,uuid),
 relation_admitted(uuid,uuid,uuid),relation_projection(uuid,uuid,uuid,uuid,uuid),relation_manifest(uuid,uuid,uuid,uuid,uuid,uuid),
 relation_endpoints(uuid,uuid,uuid,uuid,uuid),relation_publish(uuid,uuid,uuid,uuid,uuid,uuid),relation_read(uuid,uuid,uuid,uuid,int,uuid,uuid,boolean),
 relation_export_ids(),relation_intact(uuid,uuid,uuid),browser_role_safe(),retention_plan(uuid,text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_role_safe(),relation_object(uuid,uuid,uuid,uuid,uuid,bigint,text) TO rs_upload;
GRANT EXECUTE ON FUNCTION relation_scope(uuid,uuid,uuid,uuid,uuid),relation_publish(uuid,uuid,uuid,uuid,uuid,uuid),relation_read(uuid,uuid,uuid,uuid,int,uuid,uuid,boolean) TO rs_runtime;
GRANT EXECUTE ON FUNCTION relation_export_ids(),relation_intact(uuid,uuid,uuid) TO rs_retention;
GRANT SELECT(workspace_id,source_id,snapshot_id,id,actor,run_id,revision_id,manifest,digest) ON relation_result TO rs_retention;
GRANT SELECT ON relation_pair TO rs_retention;
CREATE FUNCTION relation_capacity(w uuid,src uuid,s uuid,r uuid,h uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE n int;
BEGIN
 IF NOT relation_scope(w,src,s,r,h) THEN RETURN NULL; END IF;
 SELECT cardinality(version_ids) INTO n FROM snapshot WHERE (workspace_id,source_id,id)=(w,src,s);
 RETURN jsonb_build_object('versions',n,'reason',CASE WHEN n>32 THEN 'VERSION_LIMIT'
 WHEN EXISTS(SELECT FROM snapshot_member m JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(w,src,m.version_id)
 WHERE (m.workspace_id,m.snapshot_id)=(w,s) AND octet_length(v.text_content)>4096) THEN 'TEXT_BYTE_LIMIT' END);
END $$;
REVOKE ALL ON FUNCTION relation_capacity(uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relation_capacity(uuid,uuid,uuid,uuid,uuid) TO rs_runtime;
