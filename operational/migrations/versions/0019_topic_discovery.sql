-- Every permission is specific to the admitted bytes. Preserve the 0018 business seal when off.
ALTER TABLE browser_admission ADD COLUMN topic_discovery_allowed boolean NOT NULL DEFAULT false;
CREATE OR REPLACE FUNCTION retention_access_seal() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT CASE WHEN count(*)=0 THEN retention_access_seal_v6() ELSE encode(sha256(convert_to(retention_access_seal_v6() || string_agg(v,'' ORDER BY v),'UTF8')),'hex') END
 FROM (SELECT (to_jsonb(a) - CASE WHEN a.decision_allowed THEN ARRAY[]::text[] ELSE ARRAY['decision_allowed'] END
                       - CASE WHEN a.topic_discovery_allowed THEN ARRAY[]::text[] ELSE ARRAY['topic_discovery_allowed'] END
                       - CASE WHEN a.relation_allowed THEN ARRAY[]::text[] ELSE ARRAY['relation_allowed'] END
                       - CASE WHEN a.human_review_allowed THEN ARRAY[]::text[] ELSE ARRAY['human_review_allowed'] END)::text v
 FROM browser_admission a UNION ALL
 SELECT jsonb_build_array(sl.workspace_id,sl.source_id,sl.project_id,sl.location_id,sl.external_place_id,
 s.kind,s.source_key,s.external_source_id)::text FROM source_location sl JOIN source s
 ON (s.workspace_id,s.id)=(sl.workspace_id,sl.source_id)
 WHERE EXISTS(SELECT FROM browser_admission a WHERE (a.workspace_id,a.source_id)=(s.workspace_id,s.id))) grants
$$;

CREATE FUNCTION topic_admitted(w uuid,src uuid,s uuid) RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT candidate_admitted(w,s) AND EXISTS(SELECT FROM snapshot sn
 JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,src,b.file_sha256)
 WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s) AND a.topic_discovery_allowed
 AND b.status='accepted' AND b.delivery IS NOT NULL)
$$;
CREATE FUNCTION topic_object(w uuid,src uuid,s uuid,r uuid,actor uuid,expires bigint,ticket text)
RETURNS TABLE(root text,digest text,bytes bigint,manifest text,parents text[]) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT ws.delivery_object_root,sn.duckdb_sha256::text,4194304::bigint,sn.manifest_sha256::text,
 ARRAY[b.file_sha256::text,b.delivery->>'mapping_sha256',b.delivery->>'passport_sha256',b.delivery->>'coverage_sha256']
 FROM workspace ws JOIN snapshot sn ON (sn.workspace_id,sn.source_id,sn.id)=(w,src,s)
 JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id)
 JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(w,actor) JOIN principal p ON p.id=actor
 WHERE session_user='rs_upload' AND r=s AND ws.id=w AND ws.state='active' AND retention_online()
 AND m.state='active' AND m.role IN ('owner','analyst') AND p.state='active' AND topic_admitted(w,src,s)
 AND expires BETWEEN floor(extract(epoch FROM statement_timestamp()))::bigint AND floor(extract(epoch FROM statement_timestamp()))::bigint+30
 AND EXISTS(SELECT FROM access_session x WHERE x.principal_id=actor AND x.revoked_at IS NULL AND x.expires_at>statement_timestamp()
 AND encode(sha256(x.token_hash || sha256(convert_to(concat_ws('|','topic-snapshot-1',w,src,s,r,actor,expires),'UTF8'))),'hex')=ticket)
$$;

CREATE TABLE topic_result (
 workspace_id uuid NOT NULL, source_id uuid NOT NULL, snapshot_id uuid NOT NULL, result_id uuid NOT NULL,
 project_id uuid NOT NULL, decision_id uuid, human_id uuid, binding jsonb NOT NULL, manifest jsonb NOT NULL,
 digest sha256_hex NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(workspace_id,snapshot_id,result_id), UNIQUE(workspace_id,snapshot_id,binding),
 CHECK(human_id IS NULL OR decision_id IS NOT NULL),
 FOREIGN KEY(workspace_id,source_id,snapshot_id) REFERENCES snapshot(workspace_id,source_id,id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,project_id,source_id) REFERENCES source(workspace_id,project_id,id),
 FOREIGN KEY(workspace_id,decision_id,snapshot_id,source_id) REFERENCES decision_binding(workspace_id,run_id,snapshot_id,source_id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,decision_id,human_id) REFERENCES human_review_result ON DELETE CASCADE
);
CREATE TABLE topic_input (
 workspace_id uuid NOT NULL,snapshot_id uuid NOT NULL,result_id uuid NOT NULL,version_id uuid NOT NULL,
 text_sha sha256_hex, group_sha sha256_hex, chars integer NOT NULL, state text NOT NULL, term text,
 PRIMARY KEY(workspace_id,snapshot_id,result_id,version_id),
 FOREIGN KEY(workspace_id,snapshot_id,result_id) REFERENCES topic_result ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(workspace_id,snapshot_id,version_id) REFERENCES snapshot_member ON DELETE CASCADE
);
CREATE TABLE topic_event (
 workspace_id uuid NOT NULL,snapshot_id uuid NOT NULL,result_id uuid NOT NULL,id uuid NOT NULL,
 parents uuid[] NOT NULL, action text NOT NULL CHECK(action IN ('merge','split','confirm','reject')),
 reason text NOT NULL CHECK(aspect_nonblank(reason) AND length(reason)<=300),
 actor uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 project_id uuid NOT NULL, aspect_id uuid, aspect_version_id uuid, aspect_sha sha256_hex,
 CHECK((action='confirm')=(aspect_id IS NOT NULL) AND (aspect_id IS NULL)=(aspect_version_id IS NULL) AND (aspect_id IS NULL)=(aspect_sha IS NULL)),
 PRIMARY KEY(workspace_id,snapshot_id,result_id,id),
 FOREIGN KEY(workspace_id,snapshot_id,result_id) REFERENCES topic_result ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,project_id,aspect_id,aspect_version_id,aspect_sha) REFERENCES aspect_version(workspace_id,project_id,aspect_id,version_id,digest) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE topic_candidate (
 workspace_id uuid NOT NULL,snapshot_id uuid NOT NULL,result_id uuid NOT NULL,id uuid NOT NULL DEFAULT gen_random_uuid(),
 content_key sha256_hex NOT NULL, term text, origin_event uuid, members uuid[] NOT NULL CHECK(cardinality(members) BETWEEN 1 AND 512),
 support integer NOT NULL CHECK(support BETWEEN 1 AND cardinality(members)),
 PRIMARY KEY(workspace_id,snapshot_id,result_id,id),
 FOREIGN KEY(workspace_id,snapshot_id,result_id) REFERENCES topic_result ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,snapshot_id,result_id,origin_event) REFERENCES topic_event ON DELETE CASCADE
);
CREATE TABLE topic_history (
 workspace_id uuid NOT NULL,snapshot_id uuid NOT NULL,result_id uuid NOT NULL,id uuid NOT NULL,
 candidate_id uuid NOT NULL,source_id uuid NOT NULL,history_id uuid NOT NULL, prior uuid,
 actor uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 receipt jsonb NOT NULL, digest sha256_hex NOT NULL,
 PRIMARY KEY(workspace_id,snapshot_id,result_id,id), UNIQUE(workspace_id,snapshot_id,result_id,prior),
 FOREIGN KEY(workspace_id,snapshot_id,result_id,candidate_id) REFERENCES topic_candidate ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,snapshot_id,result_id,prior) REFERENCES topic_history ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,source_id,history_id) REFERENCES snapshot(workspace_id,source_id,id) ON DELETE CASCADE
);
CREATE TABLE topic_request (
 workspace_id uuid NOT NULL,snapshot_id uuid NOT NULL,result_id uuid NOT NULL,request_key uuid NOT NULL,
 actor uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 signature sha256_hex NOT NULL, reply uuid NOT NULL,
 PRIMARY KEY(workspace_id,request_key),
 FOREIGN KEY(workspace_id,snapshot_id,result_id) REFERENCES topic_result ON DELETE CASCADE
);
CREATE FUNCTION topic_hash(d jsonb) RETURNS text LANGUAGE sql IMMUTABLE STRICT
SET search_path=pg_catalog,public,pg_temp AS $$ SELECT encode(sha256(convert_to(d::text,'UTF8')),'hex') $$;

CREATE FUNCTION topic_scope(w uuid,src uuid,s uuid,r uuid,h uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF session_user<>'rs_runtime' OR retention_online() IS NOT TRUE OR candidate_scope(w,src,s) IS NOT TRUE
 OR topic_admitted(w,src,s) IS NOT TRUE OR NOT EXISTS(SELECT FROM access_session x WHERE x.token_hash=
 sha256(convert_to(current_setting('rs.session_proof',true),'UTF8')) AND x.revoked_at IS NULL AND x.expires_at>clock_timestamp()) THEN RETURN false; END IF;
 IF r IS NULL THEN RETURN h IS NULL; END IF;
 IF decision_scope(w,src,s) IS NOT TRUE OR NOT EXISTS(SELECT FROM decision_binding b JOIN analysis_run j ON
 (j.workspace_id,j.id)=(w,r) JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=(w,r,j.output_artifact_id)
 WHERE (b.workspace_id,b.source_id,b.snapshot_id,b.run_id)=(w,src,s,r) AND j.status='succeeded'
 AND b.manifest->>'rows_sha256' IS NOT NULL AND a.storage_state='available' AND aspect_scope(w,b.project_id))
 THEN RETURN false; END IF;
 RETURN h IS NULL OR (human_scope(w,src,s,r,false) AND EXISTS
 (SELECT FROM human_review_result WHERE (workspace_id,run_id,id)=(w,r,h)));
END $$;

CREATE FUNCTION topic_capacity(w uuid,src uuid,s uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE n int; largest int; total int; d jsonb; row_hash text; ids uuid[]; frozen snapshot;
BEGIN
 IF NOT topic_scope(w,src,s,NULL,NULL) THEN RETURN NULL; END IF;
 SELECT cardinality(sn.version_ids),jsonb_build_object('N_input',b.input_count,'N_accepted',b.accepted_count,
 'N_quarantine',b.rejected_count,'N_confirmed_duplicate',b.confirmed_duplicate_count,'manifest_sha256',sn.manifest_sha256,
 'snapshot_object_sha256',sn.duckdb_sha256,'workspace_id',w,'source_id',src,'project_id',z.project_id,'snapshot_id',s)
 INTO STRICT n,d FROM snapshot sn JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id)
 JOIN source z ON (z.workspace_id,z.id)=(w,src) WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s);
 IF n<=512 THEN
 SELECT * INTO STRICT frozen FROM snapshot WHERE (workspace_id,source_id,id)=(w,src,s);
 SELECT max(octet_length(v.text_content)),sum(octet_length(v.text_content)),
 encode(sha256(convert_to(coalesce(string_agg(v.id::text||':'||v.content_sha256||':'||
 coalesce(encode(sha256(convert_to(v.text_content,'UTF8')),'hex'),'-')||chr(10),'' ORDER BY v.id),''),'UTF8')),'hex'),
 coalesce(array_agg(v.id ORDER BY v.id),'{}') INTO largest,total,row_hash,ids FROM snapshot_member m
 JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(w,src,m.version_id) WHERE (m.workspace_id,m.snapshot_id)=(w,s);
 IF frozen.version_ids IS DISTINCT FROM ids OR frozen.manifest_sha256 IS DISTINCT FROM snapshot_digest(w,src,frozen.batch_id,frozen.duckdb_sha256,frozen.filters,ids)
 OR ids IS DISTINCT FROM ARRAY(SELECT i.version_id FROM import_row i WHERE (i.workspace_id,i.source_id,i.batch_id)=(w,src,frozen.batch_id) AND status='accepted' ORDER BY i.version_id)
 THEN RAISE EXCEPTION 'invalid_topic_snapshot'; END IF;
 END IF;
 RETURN d||jsonb_build_object('source_rows_sha256',row_hash,'profile','supported-phrase-v1','reason',CASE WHEN n>512 THEN 'VERSION_LIMIT'
 WHEN largest>8192 THEN 'TEXT_BYTE_LIMIT' WHEN total>262144 THEN 'CORPUS_BYTE_LIMIT' END);
END $$;

CREATE FUNCTION topic_features(t text) RETURNS TABLE(term text, width integer) LANGUAGE sql IMMUTABLE STRICT
SET search_path=pg_catalog,public,pg_temp AS $$
 WITH tokens AS (SELECT word,lag(word) OVER(ORDER BY ord) prev
 FROM regexp_split_to_table(t COLLATE "und-x-icu",'[^[:alnum:]]+') WITH ORDINALITY split(word,ord))
 SELECT DISTINCT f.* FROM tokens CROSS JOIN LATERAL (VALUES(word,1),
 (CASE WHEN length(prev) BETWEEN 4 AND 64 THEN prev||' '||word END,2)) f(term,width)
 WHERE length(word) BETWEEN 4 AND 64 AND f.term IS NOT NULL
$$;

CREATE FUNCTION topic_projection(w uuid,src uuid,s uuid,r uuid,h uuid)
RETURNS TABLE(version_id uuid,text_sha text,group_sha text,chars int,state text,term text)
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 WITH docs AS MATERIALIZED (
 SELECT v.id, v.text_content body, btrim(regexp_replace(lower(v.text_content COLLATE "und-x-icu"),'[[:space:]]+',' ','g')) norm,
 CASE WHEN NOT coalesce(aspect_nonblank(v.text_content),false) THEN 'blank'
 WHEN r IS NULL THEN 'all'
 WHEN e.action='dispute' THEN 'disputed'
 WHEN e.outcome IN ('unclear','topic_uncertain','mixed') THEN 'human_unclear'
 WHEN e.id IS NOT NULL THEN 'excluded_human_'||e.action
 WHEN d.outcome IN ('not_mentioned','unclear','mixed','topic_uncertain') THEN 'machine_'||d.outcome::text
 WHEN d.outcome IS NULL THEN 'missing_decision'
 ELSE 'excluded_machine_'||d.outcome::text END selection
 FROM snapshot_member m JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(w,src,m.version_id)
 LEFT JOIN decision_row d ON (d.workspace_id,d.run_id,d.version_id)=(w,r,v.id)
 LEFT JOIN human_result_member hm ON (hm.workspace_id,hm.run_id,hm.result_id,hm.version_id)=(w,r,h,v.id)
 LEFT JOIN human_review_event e ON (e.workspace_id,e.run_id,e.version_id,e.id)=(w,r,v.id,hm.event_id)
 WHERE (m.workspace_id,m.source_id,m.snapshot_id)=(w,src,s)),
 unique_docs AS MATERIALIZED (SELECT DISTINCT norm FROM docs WHERE selection NOT LIKE 'excluded%' AND selection NOT IN ('blank','missing_decision')),
 features AS MATERIALIZED (SELECT u.norm,f.* FROM unique_docs u CROSS JOIN LATERAL topic_features(u.norm) f),
 ranked AS (SELECT *,count(*) OVER(PARTITION BY f.term) df FROM features f),
 best AS (SELECT DISTINCT ON (norm) norm,ranked.term FROM ranked WHERE df>=3 ORDER BY norm,width DESC,df,ranked.term COLLATE "C"),
 groups AS (SELECT norm,best.term,count(*) OVER(PARTITION BY best.term) support FROM best)
 SELECT d.id,encode(sha256(convert_to(body,'UTF8')),'hex'),CASE WHEN selection<>'blank' THEN encode(sha256(convert_to(d.norm,'UTF8')),'hex') END,
 coalesce(length(body),0),selection,CASE WHEN g.support>=3 THEN g.term END FROM docs d
 LEFT JOIN groups g ON g.norm=d.norm AND selection NOT LIKE 'excluded%' AND selection NOT IN ('blank','missing_decision')
$$;

CREATE FUNCTION topic_method() RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('method_sha256',topic_hash(jsonb_build_array(pg_get_functiondef('topic_projection(uuid,uuid,uuid,uuid,uuid)'::regprocedure),pg_get_functiondef('topic_features(text)'::regprocedure))),
 'runtime',current_setting('server_version_num'),'icu_version',pg_collation_actual_version('"und-x-icu"'::regcollation),
 'min_support',3,'unit','full_review','selection_version','all-or-single-frozen-1')
$$;

CREATE FUNCTION topic_replay(w uuid,k uuid,sig text) RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE prev topic_request;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(w::text,2519));
 SELECT * INTO prev FROM topic_request WHERE (workspace_id,request_key)=(w,k);
 IF FOUND THEN RETURN CASE WHEN prev.signature=sig AND prev.actor=access_actor() THEN human_status(200,prev.reply) ELSE human_status(409) END; END IF;
 IF (SELECT count(*) FROM topic_request WHERE workspace_id=w AND created_at>clock_timestamp()-interval '1 hour')>=128 THEN RETURN human_status(429); END IF;
 RETURN NULL;
END $$;

CREATE FUNCTION topic_key(binding jsonb,ids uuid[]) RETURNS text LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT topic_hash(jsonb_build_array('candidate-1',binding,ARRAY(SELECT x FROM unnest(ids) x ORDER BY x)))
$$;

CREATE FUNCTION topic_add_candidate(w uuid,s uuid,res uuid,scope jsonb,ids uuid[],label text,origin uuid) RETURNS uuid
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE members_sorted uuid[]; n int; saved uuid;
BEGIN
 SELECT array_agg(i.version_id ORDER BY i.version_id),count(DISTINCT group_sha) INTO members_sorted,n
 FROM topic_input i WHERE (workspace_id,snapshot_id,result_id)=(w,s,res) AND i.version_id=ANY(ids) AND term IS NOT NULL;
 IF cardinality(ids) IS DISTINCT FROM cardinality(members_sorted) OR n<1 THEN RAISE EXCEPTION 'invalid_topic_members'; END IF;
 INSERT INTO topic_candidate(workspace_id,snapshot_id,result_id,content_key,term,origin_event,members,support)
 VALUES(w,s,res,topic_key(scope,members_sorted),label,origin,members_sorted,n) RETURNING id INTO STRICT saved;
 RETURN saved;
END $$;

CREATE FUNCTION topic_publish(w uuid,src uuid,s uuid,r uuid,h uuid,k uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE scope jsonb; doc jsonb; cap jsonb; sn snapshot; res uuid; sig text; prev jsonb; grp record; ids uuid[]; expected text; actual text;
BEGIN
 IF k IS NULL OR NOT topic_scope(w,src,s,r,h) THEN RETURN NULL; END IF;
 cap:=topic_capacity(w,src,s);
 IF cap->>'reason' IS NOT NULL THEN RETURN human_status(413)||jsonb_build_object('reason',cap->>'reason'); END IF;
 sig:=topic_hash(jsonb_build_array('publish',w,src,s,r,h)); prev:=topic_replay(w,k,sig); IF prev IS NOT NULL THEN RETURN prev; END IF;
 SELECT * INTO STRICT sn FROM snapshot WHERE (workspace_id,source_id,id)=(w,src,s);
 IF sn.version_ids IS DISTINCT FROM ARRAY(SELECT m.version_id FROM snapshot_member m WHERE (m.workspace_id,m.source_id,m.snapshot_id)=(w,src,s) ORDER BY m.version_id)
 OR sn.version_ids IS DISTINCT FROM ARRAY(SELECT i.version_id FROM import_row i WHERE (i.workspace_id,i.source_id,i.batch_id)=(w,src,sn.batch_id) AND status='accepted' ORDER BY i.version_id)
 OR (r IS NOT NULL AND sn.version_ids IS DISTINCT FROM ARRAY(SELECT d.version_id FROM decision_row d WHERE (d.workspace_id,d.source_id,d.snapshot_id,d.run_id)=(w,src,s,r) ORDER BY d.version_id))
 THEN RAISE EXCEPTION 'invalid_topic_population'; END IF;
 scope:=(cap-'reason')||topic_method()||jsonb_build_object('mode',CASE WHEN r IS NULL THEN 'all' ELSE 'selected_definition' END,
 'decision_run',r,'human_revision',h);
 IF r IS NOT NULL THEN
 SELECT scope||jsonb_build_object('aspect_id',b.aspect_id,'version_id',b.version_id,'definition_sha256',b.digest,
 'rows_sha256',b.manifest->>'rows_sha256','artifact_sha256',a.checksum,'human_digest',hr.digest) INTO STRICT scope
 FROM decision_binding b JOIN analysis_run j ON (j.workspace_id,j.id)=(w,r) JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=(w,r,j.output_artifact_id)
 LEFT JOIN human_review_result hr ON (hr.workspace_id,hr.run_id,hr.id)=(w,r,h) WHERE (b.workspace_id,b.run_id)=(w,r);
 END IF;
 SELECT result_id INTO res FROM topic_result WHERE (workspace_id,snapshot_id,binding)=(w,s,scope);
 IF NOT FOUND THEN
 res:=gen_random_uuid();
 WITH proposed AS MATERIALIZED (SELECT * FROM topic_projection(w,src,s,r,h)),
 written AS (INSERT INTO topic_input SELECT w,s,res,p.* FROM proposed p RETURNING version_id,text_sha,group_sha,chars,state,term)
 SELECT (SELECT topic_hash(coalesce(jsonb_agg(to_jsonb(p) ORDER BY version_id),'[]')) FROM proposed p),
 (SELECT topic_hash(coalesce(jsonb_agg(to_jsonb(i) ORDER BY version_id),'[]')) FROM written i),
 (SELECT coalesce(array_agg(version_id ORDER BY version_id),'{}') FROM written) INTO expected,actual,ids;
 IF expected IS DISTINCT FROM actual OR ids IS DISTINCT FROM sn.version_ids THEN RAISE EXCEPTION 'incomplete_topic_publication'; END IF;
 WITH counts AS (SELECT count(*) FILTER(WHERE state='blank') blank,count(*) FILTER(WHERE state LIKE 'excluded%') excluded,
 count(*) FILTER(WHERE term IS NOT NULL) clustered,count(DISTINCT group_sha) FILTER(WHERE state NOT LIKE 'excluded%') uniq,
 count(DISTINCT term) candidates FROM topic_input WHERE (workspace_id,snapshot_id,result_id)=(w,s,res)),
 totals AS (SELECT *,cardinality(ids)-blank eligible,cardinality(ids)-blank-excluded selected FROM counts)
 SELECT scope||jsonb_build_object('status',CASE WHEN selected=0 THEN 'NO_ELIGIBLE_TEXTS' WHEN uniq<3 THEN 'INSUFFICIENT_SUPPORT'
 WHEN candidates=0 THEN 'ALL_NOISE' ELSE 'COMPLETE' END,'N_blank',blank,'N_eligible',eligible,'N_selected',selected,
 'N_excluded_from_selection',excluded,'N_clustered',clustered,'N_noise',selected-clustered,'N_unique_inputs',uniq,'N_not_processed',0,
 'candidates',candidates,'input_rows_sha256',actual,'selection_counts',(SELECT coalesce(jsonb_object_agg(state,n),'{}') FROM
 (SELECT state,count(*) n FROM topic_input WHERE (workspace_id,snapshot_id,result_id)=(w,s,res) GROUP BY state) x)) INTO doc FROM totals;
 INSERT INTO topic_result(workspace_id,source_id,snapshot_id,result_id,project_id,decision_id,human_id,binding,manifest,digest)
 VALUES(w,src,s,res,(scope->>'project_id')::uuid,r,h,scope,doc,topic_hash(doc));
 FOR grp IN SELECT term,array_agg(version_id ORDER BY version_id) members FROM topic_input WHERE (workspace_id,snapshot_id,result_id)=(w,s,res) AND term IS NOT NULL GROUP BY term LOOP
 PERFORM topic_add_candidate(w,s,res,scope,grp.members,grp.term,NULL);
 END LOOP;
 IF topic_intact(w,s,res) IS NOT TRUE THEN RAISE EXCEPTION 'incomplete_topic_candidates'; END IF;
 END IF;
 INSERT INTO topic_request(workspace_id,snapshot_id,result_id,request_key,actor,signature,reply) VALUES(w,s,res,k,access_actor(),sig,res);
 IF NOT topic_scope(w,src,s,r,h) THEN RAISE EXCEPTION 'topic_scope_closed'; END IF;
 RETURN human_status(201,res);
END $$;

CREATE FUNCTION topic_review(w uuid,src uuid,s uuid,res uuid,k uuid,act text,parents uuid[],note text,
 parts jsonb,a uuid,v uuid,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE saved topic_result; parents_sorted uuid[]; ids uuid[]; partition_ids uuid[]; piece jsonb; group_ids uuid[];
 sig text; prev jsonb; event uuid:=gen_random_uuid(); created record;
BEGIN
 SELECT * INTO saved FROM topic_result WHERE (workspace_id,source_id,snapshot_id,result_id)=(w,src,s,res);
 IF NOT FOUND OR NOT topic_scope(w,src,s,saved.decision_id,saved.human_id) THEN RETURN NULL; END IF;
 IF num_nulls(k,act,parents,note)>0 OR act NOT IN ('merge','split','confirm','reject') OR cardinality(parents) NOT BETWEEN 1 AND 8
 OR (act='merge')<>(cardinality(parents)>1) OR NOT aspect_nonblank(note) OR length(note)>300 OR (act='split')<>(parts IS NOT NULL)
 OR (a IS NULL)<>(v IS NULL) OR (act='confirm')<>(num_nonnulls(a,payload)=1) OR (act<>'confirm' AND num_nonnulls(a,payload)>0)
 THEN RETURN human_status(422); END IF;
 SELECT array_agg(DISTINCT x ORDER BY x) INTO parents_sorted FROM unnest(parents) x;
 IF cardinality(parents_sorted)<>cardinality(parents) THEN RETURN human_status(422); END IF;
 sig:=topic_hash(jsonb_build_array('review',w,src,s,res,act,parents_sorted,note,parts,a,v,payload));
 prev:=topic_replay(w,k,sig); IF prev IS NOT NULL THEN RETURN prev; END IF;
 IF (SELECT count(*) FROM topic_candidate WHERE (workspace_id,snapshot_id,result_id)=(w,s,res) AND id=ANY(parents))<>cardinality(parents)
 THEN RETURN human_status(404); END IF;
 IF EXISTS(SELECT FROM topic_event e WHERE (workspace_id,snapshot_id,result_id)=(w,s,res) AND e.parents && parents_sorted)
 THEN RETURN human_status(409); END IF;
 SELECT array_agg(DISTINCT x ORDER BY x) INTO ids FROM topic_candidate c CROSS JOIN unnest(c.members) x
 WHERE (workspace_id,snapshot_id,result_id)=(w,s,res) AND id=ANY(parents);
 IF act='split' THEN
 IF jsonb_typeof(parts)<>'array' OR jsonb_array_length(parts) NOT BETWEEN 2 AND 8 OR cardinality(ids)>96 THEN RETURN human_status(422); END IF;
 FOR piece IN SELECT value FROM jsonb_array_elements(parts) LOOP
 IF jsonb_typeof(piece)<>'array' OR jsonb_array_length(piece) NOT BETWEEN 1 AND 96 THEN RETURN human_status(422); END IF;
 END LOOP;
 SELECT array_agg(x::uuid ORDER BY x::uuid) INTO partition_ids FROM jsonb_array_elements(parts) g CROSS JOIN jsonb_array_elements_text(g) x;
 IF partition_ids IS DISTINCT FROM ids THEN RETURN human_status(422); END IF;
 ELSIF act='confirm' THEN
 IF NOT aspect_scope(w,saved.project_id) THEN RETURN NULL; END IF;
 IF payload IS NOT NULL THEN
 SELECT * INTO STRICT created FROM save_aspect(w,saved.project_id,NULL,NULL,k,payload,'manual',false);
 IF created.code<>201 THEN RETURN human_status(CASE WHEN created.code=200 THEN 409 ELSE created.code END); END IF;
 a:=created.aspect_id; v:=created.version_id;
 ELSIF NOT EXISTS(SELECT FROM aspect_version av JOIN aspect obj USING(workspace_id,project_id,aspect_id)
 WHERE (av.workspace_id,av.project_id,av.aspect_id,av.version_id)=(w,saved.project_id,a,v) AND obj.archived_at IS NULL FOR SHARE OF obj)
 THEN RETURN human_status(404); END IF;
 END IF;
 INSERT INTO topic_event(workspace_id,snapshot_id,result_id,id,parents,action,reason,actor,project_id,aspect_id,aspect_version_id,aspect_sha)
 VALUES(w,s,res,event,parents_sorted,act,note,access_actor(),saved.project_id,a,v,(SELECT digest FROM aspect_version av WHERE av.version_id=v));
 IF NOT FOUND THEN RAISE EXCEPTION 'missing_topic_event'; END IF;
 IF act='merge' THEN PERFORM topic_add_candidate(w,s,res,saved.binding,ids,NULL,event);
 ELSIF act='split' THEN
 FOR piece IN SELECT value FROM jsonb_array_elements(parts) LOOP
 SELECT array_agg(x::uuid) INTO group_ids FROM jsonb_array_elements_text(piece) x;
 PERFORM topic_add_candidate(w,s,res,saved.binding,group_ids,NULL,event);
 END LOOP;
 END IF;
 INSERT INTO topic_request(workspace_id,snapshot_id,result_id,request_key,actor,signature,reply) VALUES(w,s,res,k,access_actor(),sig,event);
 IF NOT topic_scope(w,src,s,saved.decision_id,saved.human_id) THEN RAISE EXCEPTION 'topic_scope_closed'; END IF;
 IF act IN ('merge','split') AND ids IS DISTINCT FROM ARRAY(SELECT x FROM topic_candidate c CROSS JOIN unnest(c.members) x
 WHERE (c.workspace_id,c.snapshot_id,c.result_id,c.origin_event)=(w,s,res,event) ORDER BY x)
 THEN RAISE EXCEPTION 'invalid_topic_lineage'; END IF;
 RETURN human_status(201,event);
END $$;

CREATE FUNCTION topic_period(w uuid,src uuid,s uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 WITH dates AS (
 SELECT coalesce(v.published_date,(v.published_at AT TIME ZONE 'UTC')::date) AS day FROM snapshot_member m JOIN review_version v
 ON (v.workspace_id,v.source_id,v.id)=(w,src,m.version_id) WHERE (m.workspace_id,m.source_id,m.snapshot_id)=(w,src,s))
 SELECT jsonb_build_object('snapshot_id',s,'manifest_sha256',sn.manifest_sha256,'snapshot_object_sha256',sn.duckdb_sha256,
 'known_dates',count(d.day),'N',cardinality(sn.version_ids),'first_day',min(d.day),'last_day',max(d.day),
 'coverage',b.coverage,'from',b.covered_from,'until',b.covered_until,'filter_digest',topic_hash(b.delivery#>'{coverage_passport,filters}'),
 'mode',b.delivery#>'{coverage_passport,mode}','basis_digest',topic_hash(b.delivery#>'{coverage_passport,basis}'),
 'quarantine',b.rejected_count,'duplicates',b.confirmed_duplicate_count)
 FROM snapshot sn JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,sn.batch_id) LEFT JOIN dates d ON true
 WHERE (sn.workspace_id,sn.source_id,sn.id)=(w,src,s) GROUP BY sn.workspace_id,sn.source_id,sn.id,b.workspace_id,b.source_id,b.id
$$;

CREATE FUNCTION topic_history_check(w uuid,src uuid,s uuid,res uuid,c uuid,hs uuid,k uuid,prior_id uuid,
 witness uuid,accept boolean) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE saved topic_result; cand topic_candidate; cap jsonb; current_period jsonb; historical jsonb; state text; hits jsonb:='[]';
 checked int:=0; base topic_history; doc jsonb; sig text; prev jsonb; rid uuid:=gen_random_uuid();
BEGIN
 SELECT * INTO saved FROM topic_result WHERE (workspace_id,source_id,snapshot_id,result_id)=(w,src,s,res);
 IF NOT FOUND OR NOT topic_scope(w,src,s,saved.decision_id,saved.human_id) OR NOT topic_scope(w,src,hs,NULL,NULL) THEN RETURN NULL; END IF;
 SELECT * INTO cand FROM topic_candidate WHERE (workspace_id,snapshot_id,result_id,id)=(w,s,res,c);
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF k IS NULL OR hs=s OR num_nonnulls(prior_id,witness,accept) NOT IN (0,3) THEN RETURN human_status(422); END IF;
 sig:=topic_hash(jsonb_build_array('history',w,src,s,res,c,hs,prior_id,witness,accept));
 prev:=topic_replay(w,k,sig); IF prev IS NOT NULL THEN RETURN prev; END IF;
 cap:=topic_capacity(w,src,hs);
 IF cap->>'reason' IS NOT NULL THEN RETURN human_status(413)||jsonb_build_object('reason',cap->>'reason'); END IF;
 IF prior_id IS NOT NULL THEN
 SELECT * INTO base FROM topic_history WHERE (workspace_id,snapshot_id,result_id,candidate_id,history_id,id)=(w,s,res,c,hs,prior_id) AND prior IS NULL;
 IF NOT FOUND OR NOT EXISTS(SELECT FROM jsonb_array_elements(base.receipt->'hits') x WHERE x->>'version_id'=witness::text) THEN RETURN human_status(422); END IF;
 IF EXISTS(SELECT FROM topic_history WHERE (workspace_id,snapshot_id,result_id,prior)=(w,s,res,prior_id)) THEN RETURN human_status(409); END IF;
 doc:=jsonb_build_object('state',CASE WHEN accept THEN 'OBSERVED_IN_SELECTED_HISTORY' ELSE 'ANALOGY_REJECTED' END,
 'version_id',witness,'check_sha256',base.digest,'candidate_id',c,'history_id',hs);
 ELSE
 current_period:=topic_period(w,src,s); historical:=topic_period(w,src,hs);
 IF NOT saved.binding @> topic_method() THEN state:='HISTORY_NOT_COMPARABLE';
 ELSIF (historical->>'known_dates')::int=0 OR (current_period->>'known_dates')::int=0 THEN state:='HISTORY_NOT_AVAILABLE';
 ELSIF (historical->>'last_day')::date >= (current_period->>'first_day')::date
 OR (historical->'filter_digest',historical->'basis_digest',historical->'mode') IS DISTINCT FROM (current_period->'filter_digest',current_period->'basis_digest',current_period->'mode')
 OR (historical->>'until')::timestamptz>(current_period->>'from')::timestamptz THEN state:='HISTORY_NOT_COMPARABLE';
 ELSE
 state:=CASE WHEN EXISTS(SELECT FROM unnest(ARRAY[historical,current_period]) z WHERE z->>'coverage' IS DISTINCT FROM 'full'
 OR z->>'known_dates'<>z->>'N' OR (z->>'quarantine')::int>0 OR (z->>'duplicates')::int>0 OR z->>'mode' IS DISTINCT FROM 'period' OR z->>'from' IS NULL OR z->>'until' IS NULL
 OR (z->>'first_day')::date<((z->>'from')::timestamptz AT TIME ZONE 'UTC')::date
 OR (z->>'last_day')::date>=((z->>'until')::timestamptz AT TIME ZONE 'UTC')::date)
 THEN 'HISTORY_PARTIAL' ELSE 'HISTORY_CHECKED' END;
 WITH reps AS (SELECT min(i.version_id::text)::uuid v FROM topic_input i WHERE (workspace_id,snapshot_id,result_id)=(w,s,res)
 AND i.version_id=ANY(cand.members) GROUP BY group_sha ORDER BY 1 LIMIT 5),
 query_features AS MATERIALIZED (SELECT DISTINCT f.term FROM reps JOIN review_version rv ON (rv.workspace_id,rv.source_id,rv.id)=(w,src,reps.v)
 CROSS JOIN LATERAL topic_features(lower(rv.text_content COLLATE "und-x-icu")) f),
 docs AS MATERIALIZED (SELECT v.id,v.text_content body FROM snapshot_member m JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(w,src,m.version_id)
 WHERE (m.workspace_id,m.snapshot_id)=(w,hs) AND coalesce(v.published_date,(v.published_at AT TIME ZONE 'UTC')::date)<(current_period->>'first_day')::date
 AND aspect_nonblank(v.text_content)),
 ranked AS (SELECT d.id,count(*) matches FROM docs d CROSS JOIN LATERAL topic_features(lower(body COLLATE "und-x-icu")) f
 JOIN query_features q ON q.term=f.term GROUP BY d.id ORDER BY matches DESC,d.id LIMIT 5)
 SELECT coalesce(jsonb_agg(jsonb_build_object('version_id',d.id,'shared_features',matches,'start',0,'end',length(d.body),
 'text_sha256',encode(sha256(convert_to(d.body,'UTF8')),'hex')) ORDER BY matches DESC,d.id),'[]'),(SELECT count(*) FROM docs)
 INTO hits,checked FROM ranked JOIN docs d USING(id);
 END IF;
 doc:=jsonb_build_object('state',state,'current_period',current_period,'historical_period',historical,'method','representative-overlap-1',
 'compatible_profile',saved.binding @> topic_method(),'N_searched',checked,'hits',hits,'candidate_key',cand.content_key,'candidate_id',c,'history_id',hs);
 END IF;
 INSERT INTO topic_history(workspace_id,snapshot_id,result_id,id,candidate_id,source_id,history_id,prior,actor,receipt,digest)
 VALUES(w,s,res,rid,c,src,hs,prior_id,access_actor(),doc,topic_hash(doc));
 INSERT INTO topic_request(workspace_id,snapshot_id,result_id,request_key,actor,signature,reply) VALUES(w,s,res,k,access_actor(),sig,rid);
 IF NOT topic_scope(w,src,s,saved.decision_id,saved.human_id) OR NOT topic_scope(w,src,hs,NULL,NULL) THEN RAISE EXCEPTION 'topic_scope_closed'; END IF;
 RETURN human_status(201,rid);
END $$;

CREATE FUNCTION topic_catalog(w uuid,src uuid,s uuid,r uuid,h uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE cap jsonb;
BEGIN
 IF NOT topic_scope(w,src,s,r,h) THEN RETURN NULL; END IF;
 cap:=topic_capacity(w,src,s);
 RETURN cap||jsonb_build_object('results',coalesce((SELECT jsonb_agg(jsonb_build_object('id',result_id,'digest',digest) ORDER BY created_at DESC)
 FROM topic_result WHERE (workspace_id,source_id,snapshot_id)=(w,src,s) AND decision_id IS NOT DISTINCT FROM r AND human_id IS NOT DISTINCT FROM h),'[]'));
END $$;

CREATE FUNCTION topic_read(w uuid,src uuid,s uuid,res uuid,c uuid,hs uuid,skip int) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE saved topic_result; cand topic_candidate; items jsonb; events jsonb; children jsonb; history jsonb; reps jsonb; total int;
BEGIN
 IF skip IS NULL OR skip NOT BETWEEN 0 AND 10000 THEN RETURN NULL; END IF;
 SELECT * INTO saved FROM topic_result WHERE (workspace_id,source_id,snapshot_id,result_id)=(w,src,s,res);
 IF NOT FOUND OR NOT topic_scope(w,src,s,saved.decision_id,saved.human_id) OR (hs IS NOT NULL AND (c IS NULL OR NOT topic_scope(w,src,hs,NULL,NULL))) THEN RETURN NULL; END IF;
 IF saved.digest<>topic_hash(saved.manifest) THEN RAISE EXCEPTION 'invalid_topic_manifest'; END IF;
 IF c IS NULL THEN
 SELECT count(*) INTO total FROM topic_candidate WHERE (workspace_id,snapshot_id,result_id)=(w,s,res);
 SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY x.id),'[]') INTO items FROM (
 SELECT p.id,p.content_key,p.term,p.support,cardinality(p.members) versions,p.origin_event,coalesce((
 SELECT e.action FROM topic_event e WHERE (e.workspace_id,e.snapshot_id,e.result_id)=(w,s,res) AND p.id=ANY(e.parents)),'unconfirmed') state
 FROM topic_candidate p WHERE (p.workspace_id,p.snapshot_id,p.result_id)=(w,s,res) ORDER BY p.id OFFSET skip LIMIT 25) x;
 ELSE
 SELECT * INTO cand FROM topic_candidate WHERE (workspace_id,snapshot_id,result_id,id)=(w,s,res,c);
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF cand.content_key<>topic_key(saved.binding,cand.members) THEN RAISE EXCEPTION 'invalid_topic_candidate'; END IF;
 total:=cardinality(cand.members);
 SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY x.version_id),'[]') INTO items FROM (
 SELECT i.version_id,i.text_sha,i.group_sha,i.state,0 AS start,i.chars AS end FROM topic_input i WHERE (workspace_id,snapshot_id,result_id)=(w,s,res)
 AND i.version_id=ANY(cand.members) ORDER BY i.version_id OFFSET skip LIMIT 25) x;
 SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY x.version_id),'[]') INTO reps FROM (
 SELECT i.version_id,i.text_sha,0 AS start,i.chars AS end FROM topic_input i JOIN
 (SELECT min(version_id::text)::uuid id FROM topic_input WHERE (workspace_id,snapshot_id,result_id)=(w,s,res) AND version_id=ANY(cand.members)
 GROUP BY group_sha ORDER BY 1 LIMIT 5) g ON g.id=i.version_id WHERE (workspace_id,snapshot_id,result_id)=(w,s,res)) x;
 SELECT coalesce(jsonb_agg(to_jsonb(e)-ARRAY['workspace_id','snapshot_id','result_id'] ORDER BY e.created_at,e.id),'[]') INTO events FROM topic_event e
 WHERE (e.workspace_id,e.snapshot_id,e.result_id)=(w,s,res) AND (e.id=cand.origin_event OR c=ANY(e.parents));
 SELECT coalesce(jsonb_agg(p.id ORDER BY p.id),'[]') INTO children FROM topic_candidate p JOIN topic_event e
 ON (e.workspace_id,e.snapshot_id,e.result_id,e.id)=(w,s,res,p.origin_event) WHERE (p.workspace_id,p.snapshot_id,p.result_id)=(w,s,res) AND c=ANY(e.parents);
 IF hs IS NOT NULL THEN
 IF EXISTS(SELECT FROM topic_history WHERE (workspace_id,snapshot_id,result_id,candidate_id,history_id)=(w,s,res,c,hs)
 AND digest<>topic_hash(receipt)) THEN RAISE EXCEPTION 'invalid_topic_check'; END IF;
 SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY x.created_at DESC,x.id),'[]') INTO history FROM
 (SELECT id,prior,actor,created_at,receipt,digest FROM topic_history WHERE (workspace_id,snapshot_id,result_id,candidate_id,history_id)=(w,s,res,c,hs)
 ORDER BY created_at DESC,id OFFSET skip LIMIT 5) x;
 END IF;
 END IF;
 RETURN jsonb_build_object('id',res,'digest',saved.digest,'manifest',saved.manifest,'candidate_id',c,
 'content_key',cand.content_key,'term',cand.term,'support',cand.support,'total',total,'offset',skip,'rows',items,
 'representatives',reps,'events',events,'children',children,'history',history);
END $$;

CREATE FUNCTION topic_export_results() RETURNS TABLE(w uuid,s uuid,res uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT t.workspace_id,t.snapshot_id,t.result_id FROM topic_result t WHERE session_user='rs_retention'
 AND (t.workspace_id,t.source_id) IN (SELECT * FROM retention_export_sources()) AND topic_admitted(t.workspace_id,t.source_id,t.snapshot_id)
 AND (t.decision_id IS NULL OR decision_exportable(t.workspace_id,t.decision_id))
 AND (t.human_id IS NULL OR (t.workspace_id,t.decision_id) IN (SELECT * FROM human_export_runs()))
$$;
CREATE FUNCTION topic_export_history() RETURNS TABLE(w uuid,s uuid,res uuid,id uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT t.workspace_id,t.snapshot_id,t.result_id,t.id FROM topic_history t WHERE
 (t.workspace_id,t.snapshot_id,t.result_id) IN (SELECT * FROM topic_export_results()) AND topic_admitted(t.workspace_id,t.source_id,t.history_id)
$$;
CREATE FUNCTION topic_intact(w uuid,s uuid,res uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT r.digest=topic_hash(r.manifest) AND r.manifest->>'input_rows_sha256'=(SELECT topic_hash(coalesce(jsonb_agg(
 to_jsonb(i)-ARRAY['workspace_id','snapshot_id','result_id'] ORDER BY i.version_id),'[]')) FROM topic_input i WHERE
 (i.workspace_id,i.snapshot_id,i.result_id)=(w,s,res))
 AND (SELECT count(*) FROM topic_candidate c WHERE (c.workspace_id,c.snapshot_id,c.result_id)=(w,s,res) AND c.origin_event IS NULL)=(r.manifest->>'candidates')::int
 AND NOT EXISTS(SELECT i.term,array_agg(i.version_id ORDER BY i.version_id) FROM topic_input i WHERE
 (i.workspace_id,i.snapshot_id,i.result_id)=(w,s,res) AND i.term IS NOT NULL GROUP BY i.term
 EXCEPT SELECT c.term,c.members FROM topic_candidate c WHERE (c.workspace_id,c.snapshot_id,c.result_id)=(w,s,res) AND c.origin_event IS NULL)
 AND NOT EXISTS(SELECT FROM topic_candidate c WHERE (c.workspace_id,c.snapshot_id,c.result_id)=(w,s,res) AND
 (c.content_key<>topic_key(r.binding,c.members) OR cardinality(c.members)<>(SELECT count(*) FROM topic_input i WHERE (i.workspace_id,i.snapshot_id,i.result_id)=(w,s,res)
 AND i.version_id=ANY(c.members) AND i.term IS NOT NULL) OR c.support<>(SELECT count(DISTINCT group_sha)
 FROM topic_input i WHERE (i.workspace_id,i.snapshot_id,i.result_id)=(w,s,res) AND i.version_id=ANY(c.members) AND i.term IS NOT NULL)))
 AND NOT EXISTS(SELECT FROM topic_history h WHERE (h.workspace_id,h.snapshot_id,h.result_id)=(w,s,res) AND h.digest<>topic_hash(h.receipt))
 FROM topic_result r WHERE (workspace_id,snapshot_id,result_id)=(w,s,res)
 AND (session_user<>'rs_retention' OR (w,s,res) IN (SELECT * FROM topic_export_results()))
$$;
ALTER FUNCTION retention_plan(uuid,text,uuid) RENAME TO pre_topic_retention_plan;
CREATE FUNCTION retention_plan(w uuid,k text,t uuid) RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE doc jsonb:=pre_topic_retention_plan(w,k,t); tab text; n bigint;
BEGIN
 FOREACH tab IN ARRAY ARRAY['topic_result','topic_input','topic_event','topic_candidate','topic_history','topic_request'] LOOP
 EXECUTE format('SELECT count(*) FROM %I x JOIN topic_result r USING(workspace_id,snapshot_id,result_id)
 WHERE r.workspace_id=$1 AND r.source_id IN (SELECT retention_sources($1,$2,$3))',tab) INTO n USING w,k,t;
 doc:=jsonb_set(doc,ARRAY['tables',tab],to_jsonb(n));
 END LOOP;
 RETURN doc;
END $$;
DO $$
DECLARE tab text; fn regprocedure;
BEGIN
 FOREACH tab IN ARRAY ARRAY['topic_result','topic_input','topic_event','topic_candidate','topic_history','topic_request'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tab);
 EXECUTE format('REVOKE ALL ON %I FROM PUBLIC,rs_runtime,rs_worker,rs_upload,rs_retention',tab);
 IF tab<>'topic_request' THEN
 EXECUTE format('CREATE POLICY owner_export ON %I FOR SELECT TO rs_retention USING (%s)',tab,
 CASE WHEN tab='topic_history' THEN '(workspace_id,snapshot_id,result_id,id) IN (SELECT * FROM topic_export_history())'
 ELSE '(workspace_id,snapshot_id,result_id) IN (SELECT * FROM topic_export_results())' END);
 EXECUTE format('GRANT SELECT ON %I TO rs_retention',tab);
 END IF;
 EXECUTE format('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_history_change()',tab);
 EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION reject_history_change()',tab);
 END LOOP;
 FOR fn IN SELECT p.oid::regprocedure FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND starts_with(p.proname,'topic_') LOOP
 EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',fn);
 END LOOP;
END $$;
ALTER FUNCTION browser_role_safe() RENAME TO pre_topic_browser_role_safe;
REVOKE EXECUTE ON FUNCTION pre_topic_browser_role_safe() FROM rs_upload;
CREATE FUNCTION browser_role_safe() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT pre_topic_browser_role_safe() AND (SELECT count(*)=6 AND bool_and(c.relrowsecurity AND
 NOT has_any_column_privilege('rs_upload',c.oid,'SELECT,INSERT,UPDATE,REFERENCES') AND NOT has_table_privilege('rs_upload',c.oid,'DELETE,TRUNCATE,TRIGGER'))
 FROM pg_class c WHERE c.relnamespace='public'::regnamespace AND c.relkind='r' AND starts_with(c.relname,'topic_'))
$$;
REVOKE ALL ON FUNCTION browser_role_safe(),retention_plan(uuid,text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_role_safe(),topic_object(uuid,uuid,uuid,uuid,uuid,bigint,text) TO rs_upload;
GRANT EXECUTE ON FUNCTION topic_catalog(uuid,uuid,uuid,uuid,uuid),topic_publish(uuid,uuid,uuid,uuid,uuid,uuid),
 topic_review(uuid,uuid,uuid,uuid,uuid,text,uuid[],text,jsonb,uuid,uuid,jsonb),
 topic_read(uuid,uuid,uuid,uuid,uuid,uuid,int),topic_history_check(uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,boolean) TO rs_runtime;
GRANT EXECUTE ON FUNCTION topic_export_results(),topic_export_history(),topic_intact(uuid,uuid,uuid) TO rs_retention;
