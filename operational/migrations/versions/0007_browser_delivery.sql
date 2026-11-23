-- The offline custodian admits reviewed synthetic bytes for a specific source.
CREATE TABLE browser_admission (
 workspace_id uuid NOT NULL, source_id uuid NOT NULL, checksum sha256_hex NOT NULL,
 PRIMARY KEY(workspace_id,source_id,checksum),
 FOREIGN KEY(workspace_id,source_id) REFERENCES source ON DELETE CASCADE
);
CREATE TABLE browser_delivery (
 workspace_id uuid NOT NULL, id uuid NOT NULL, source_id uuid NOT NULL,
 actor uuid NOT NULL REFERENCES principal, fmt text NOT NULL CHECK(fmt IN ('csv','json')),
 locations uuid[] NOT NULL CHECK(cardinality(locations) BETWEEN 1 AND 64),
 checksum sha256_hex NOT NULL, fingerprint sha256_hex NOT NULL, diagnostics jsonb NOT NULL,
 expires_at timestamptz NOT NULL, snapshot_id uuid,
 approval_hash bytea, approval_session bytea, approved_until timestamptz,
 PRIMARY KEY(workspace_id,id),
 FOREIGN KEY(workspace_id,source_id) REFERENCES source ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,source_id,snapshot_id) REFERENCES snapshot(workspace_id,source_id,id) ON DELETE CASCADE
);
CREATE INDEX browser_pending_by_actor ON browser_delivery(workspace_id,actor,expires_at) WHERE snapshot_id IS NULL;
ALTER TABLE browser_admission ENABLE ROW LEVEL SECURITY;
ALTER TABLE browser_delivery ENABLE ROW LEVEL SECURITY;

CREATE TABLE browser_summary (
 workspace_id uuid NOT NULL, run_id uuid NOT NULL, artifact_id uuid NOT NULL,
 metrics jsonb NOT NULL, PRIMARY KEY(workspace_id,artifact_id),
 FOREIGN KEY(workspace_id,run_id,artifact_id) REFERENCES artifact(workspace_id,run_id,id) ON DELETE CASCADE
);
ALTER TABLE browser_summary ENABLE ROW LEVEL SECURITY;
CREATE POLICY visible_summary ON browser_summary FOR SELECT TO rs_runtime USING (EXISTS (
 SELECT FROM artifact a WHERE (a.workspace_id,a.run_id,a.id)=(browser_summary.workspace_id,browser_summary.run_id,browser_summary.artifact_id)
));
GRANT SELECT ON browser_summary TO rs_runtime;

CREATE FUNCTION browser_member(w uuid,s uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT retention_online() AND EXISTS(
 SELECT FROM workspace_membership m JOIN principal p ON p.id=m.principal_id JOIN workspace ws ON ws.id=m.workspace_id
 JOIN source src ON (src.workspace_id,src.id)=(w,s)
 WHERE m.workspace_id=w AND m.principal_id=access_actor() AND m.state='active' AND p.state='active'
 AND ws.state='active' AND m.role IN ('owner','analyst') AND src.kind='synthetic_demo'
 AND EXISTS(SELECT FROM browser_admission a WHERE (a.workspace_id,a.source_id)=(w,s)))
$$;
CREATE FUNCTION browser_targets(w uuid) RETURNS TABLE(source_id uuid,source_label text,project_id uuid,
 project_name text,location_id uuid,location_name text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT s.id,s.origin_label::text,p.id,p.name::text,l.id,l.name::text FROM source s
 JOIN client_project p ON (p.workspace_id,p.id)=(s.workspace_id,s.project_id)
 JOIN source_location sl ON (sl.workspace_id,sl.source_id)=(s.workspace_id,s.id)
 JOIN location l ON (l.workspace_id,l.project_id,l.id)=(w,s.project_id,sl.location_id)
 WHERE s.workspace_id=w AND browser_member(w,s.id)
 ORDER BY p.id,s.id,l.id
$$;
CREATE FUNCTION browser_allowed(w uuid,s uuid,h text,loc uuid[]) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT browser_member(w,s) AND EXISTS(SELECT FROM browser_admission WHERE (workspace_id,source_id,checksum)=(w,s,h))
 AND cardinality(loc) BETWEEN 1 AND 64 AND NOT EXISTS(SELECT unnest(loc) EXCEPT
 SELECT location_id FROM source_location WHERE (workspace_id,source_id)=(w,s))
$$;
CREATE FUNCTION browser_bindings(w uuid,s uuid,h text,loc uuid[]) RETURNS TABLE(source_key text,place_key text)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT coalesce(src.external_source_id,src.source_key)::text,sl.external_place_id::text FROM source src JOIN source_location sl
 ON (src.workspace_id,src.id)=(sl.workspace_id,sl.source_id) WHERE (src.workspace_id,src.id)=(w,s) AND sl.location_id=ANY(loc)
 AND browser_allowed(w,s,h,loc)
$$;
REVOKE ALL ON FUNCTION browser_bindings(uuid,uuid,text,uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_bindings(uuid,uuid,text,uuid[]) TO rs_runtime;
CREATE FUNCTION browser_diagnostics_safe(report jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE
 SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE nums constant text[] := ARRAY['total_input_rows','accepted','quarantined','confirmed_duplicates','warning_count','missing_author_count','missing_date_count','missing_text_count','missing_review_id_count','missing_rating_count','rating_excluded_count'];
 codes constant text[] := ARRAY['ambiguous_time','binding_mismatch','confirmed_duplicate','conflicting_id','conflicting_mapping','duplicate_header','duplicate_key','empty_input','ineligible_event_basis','invalid_arguments','invalid_csv','invalid_date','invalid_encoding','invalid_field_type','invalid_format','invalid_header','invalid_json','invalid_json_item','invalid_mapping','invalid_passport','invalid_rating','invalid_shape','invalid_url','io_error','missing_date','missing_place','missing_rating','missing_review_id','missing_reviewer','missing_source','missing_text','nonexistent_time','observation_mismatch','over_limit','private_output','row_width','unexpected_column','unknown_column','unknown_timezone','unmapped_scale'];
 summary jsonb := report->'summary'; item jsonb; pos integer := 0;
BEGIN
 IF jsonb_typeof(report) IS DISTINCT FROM 'object' OR report-ARRAY['summary','positions']<>'{}'
 OR jsonb_typeof(summary) IS DISTINCT FROM 'object' OR jsonb_typeof(report->'positions') IS DISTINCT FROM 'array'
 OR jsonb_typeof(summary->'reason_counts') IS DISTINCT FROM 'object' THEN RETURN false; END IF;
 IF summary-(nums||ARRAY['reason_counts'])<>'{}' OR NOT summary ?& nums
 OR EXISTS(SELECT FROM jsonb_each(summary-'reason_counts') n WHERE n.value::text !~ '^[0-9]{1,6}$')
 OR EXISTS(SELECT FROM jsonb_each(summary->'reason_counts') n WHERE NOT n.key=ANY(codes) OR n.value::text !~ '^[0-9]{1,6}$')
 THEN RETURN false; END IF;
 FOR item IN SELECT jsonb_array_elements(report->'positions') LOOP
  pos:=pos+1;
  IF jsonb_typeof(item) IS DISTINCT FROM 'object' OR item-ARRAY['position','status','reasons','warnings','related_position']<>'{}'
   OR NOT item ?& ARRAY['position','status','reasons','warnings','related_position'] OR item->'position' IS DISTINCT FROM to_jsonb(pos)
   OR item->>'status' NOT IN ('accepted','quarantined','confirmed_duplicate') OR jsonb_typeof(item->'status') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'reasons') IS DISTINCT FROM 'array' OR jsonb_typeof(item->'warnings') IS DISTINCT FROM 'array'
   OR (item->'related_position')::text !~ '^(null|[1-9][0-9]{0,4})$' THEN RETURN false; END IF;
  IF EXISTS(SELECT FROM jsonb_array_elements_text((item->'reasons')||(item->'warnings')) r(code)
    WHERE r.code IS NULL OR NOT r.code=ANY(codes)) THEN RETURN false; END IF;
 END LOOP;
 RETURN pos BETWEEN 1 AND 10000 AND (summary->>'total_input_rows')::int=pos;
END $$;
REVOKE ALL ON FUNCTION browser_diagnostics_safe(jsonb) FROM PUBLIC;

CREATE FUNCTION browser_preview(w uuid,s uuid,d uuid,f text,loc uuid[],h text,fp text,report jsonb)
 RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE previous browser_delivery;
BEGIN
 IF NOT browser_allowed(w,s,h,loc) THEN RETURN 404; END IF;
 -- Serialize quotas and refreshes with other writers in this workspace.
 PERFORM FROM workspace WHERE id=w FOR UPDATE;
 IF NOT browser_allowed(w,s,h,loc) THEN RETURN 404; END IF;
 DELETE FROM browser_delivery WHERE workspace_id=w AND snapshot_id IS NULL AND expires_at <= clock_timestamp();
 SELECT * INTO previous FROM browser_delivery WHERE (workspace_id,id)=(w,d);
 IF FOUND THEN
   IF (previous.actor,previous.source_id) IS DISTINCT FROM (access_actor(),s) THEN RETURN 404; END IF;
   IF previous.snapshot_id IS NOT NULL THEN RETURN 409; END IF;
 ELSIF (SELECT count(*) FROM browser_delivery WHERE workspace_id=w AND snapshot_id IS NULL)>=16
 OR (SELECT count(*) FROM browser_delivery WHERE workspace_id=w)>=256 THEN RETURN 429;
 END IF;
 IF jsonb_typeof(report->'positions') IS DISTINCT FROM 'array' OR jsonb_array_length(report->'positions')>10000
 OR pg_column_size(report)>4194304 THEN RETURN 413; END IF;
 IF NOT browser_diagnostics_safe(report) THEN RETURN 422; END IF;
 INSERT INTO browser_delivery(workspace_id,id,source_id,actor,fmt,locations,checksum,fingerprint,diagnostics,expires_at,snapshot_id) VALUES(w,d,s,access_actor(),f,loc,h,fp,report,clock_timestamp()+interval '30 minutes',NULL)
 ON CONFLICT(workspace_id,id) DO UPDATE SET fmt=excluded.fmt,locations=excluded.locations,
 checksum=excluded.checksum,fingerprint=excluded.fingerprint,diagnostics=excluded.diagnostics,expires_at=excluded.expires_at,approval_hash=NULL,approval_session=NULL,approved_until=NULL;
 RETURN 200;
END $$;
CREATE FUNCTION browser_drafts(w uuid,s uuid,d uuid) RETURNS TABLE(id uuid,fmt text,locations uuid[],
 fingerprint text,diagnostics jsonb,expires_at timestamptz,snapshot_id uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT x.id,x.fmt,x.locations,x.fingerprint::text,
 CASE WHEN d IS NULL THEN x.diagnostics-'positions' ELSE x.diagnostics END,x.expires_at,x.snapshot_id
 FROM browser_delivery x WHERE (x.workspace_id,x.source_id,x.actor)=(w,s,access_actor())
 AND browser_allowed(w,s,x.checksum,x.locations) AND (d IS NULL OR x.id=d)
 AND (x.expires_at>now() OR x.snapshot_id IS NOT NULL) ORDER BY x.expires_at DESC,x.id LIMIT 50
$$;
REVOKE ALL ON FUNCTION browser_member(uuid,uuid),browser_targets(uuid),browser_allowed(uuid,uuid,text,uuid[]),
 browser_preview(uuid,uuid,uuid,text,uuid[],text,text,jsonb),browser_drafts(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_targets(uuid),browser_member(uuid,uuid),browser_allowed(uuid,uuid,text,uuid[]),
 browser_preview(uuid,uuid,uuid,text,uuid[],text,text,jsonb),browser_drafts(uuid,uuid,uuid) TO rs_runtime;

-- Include admission changes and selected place bindings in the independent current-access seal.
ALTER FUNCTION retention_access_seal() RENAME TO retention_access_seal_v6;
CREATE FUNCTION retention_access_seal() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT CASE WHEN count(*)=0 THEN retention_access_seal_v6() ELSE encode(sha256(convert_to(retention_access_seal_v6() || string_agg(v,'' ORDER BY v),'UTF8')),'hex') END
 FROM (SELECT to_jsonb(a)::text v FROM browser_admission a UNION ALL
 SELECT jsonb_build_array(sl.workspace_id,sl.source_id,sl.project_id,sl.location_id,sl.external_place_id,
 s.kind,s.source_key,s.external_source_id)::text FROM source_location sl JOIN source s
 ON (s.workspace_id,s.id)=(sl.workspace_id,sl.source_id)
 WHERE EXISTS(SELECT FROM browser_admission a WHERE (a.workspace_id,a.source_id)=(s.workspace_id,s.id))) grants
$$;
REVOKE ALL ON FUNCTION retention_access_seal() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION retention_access_seal() TO rs_retention;
ALTER FUNCTION retention_plan(uuid,text,uuid) RENAME TO retention_plan_v6;
CREATE FUNCTION retention_plan(w uuid,k text,t uuid) RETURNS jsonb LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 WITH plan AS (SELECT retention_plan_v6(w,k,t) p), targets AS MATERIALIZED(SELECT retention_sources(w,k,t) id)
 SELECT jsonb_set(p,'{tables}',(p->'tables') || jsonb_build_object(
 'browser_admission',(SELECT count(*) FROM browser_admission WHERE workspace_id=w AND source_id IN (SELECT id FROM targets)),
 'browser_delivery',(SELECT count(*) FROM browser_delivery WHERE workspace_id=w AND source_id IN (SELECT id FROM targets)),
 'browser_summary',(SELECT count(*) FROM browser_summary m JOIN analysis_run r ON (r.workspace_id,r.id)=(m.workspace_id,m.run_id)
 JOIN snapshot sn ON (sn.workspace_id,sn.id)=(r.workspace_id,r.snapshot_id) WHERE m.workspace_id=w AND sn.source_id IN (SELECT id FROM targets)))) FROM plan
$$;
REVOKE ALL ON FUNCTION retention_plan(uuid,text,uuid) FROM PUBLIC;

-- Only the separate importer login gets scoped INSERT. No login for HTTP can become this role.
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles r WHERE rolname='rs_upload' AND rolcanlogin
 AND NOT (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication)
 AND NOT EXISTS(SELECT FROM pg_auth_members WHERE member=r.oid)
 AND NOT has_schema_privilege(r.oid,'public','CREATE')
 AND NOT has_database_privilege(r.oid,current_database(),'CREATE,TEMP')) THEN
 RAISE EXCEPTION 'restricted_upload_login_required'; END IF;
END $$;
CREATE FUNCTION browser_approve(w uuid,s uuid,d uuid,h text,fp text,f text,loc uuid[],ticket bytea)
 RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE x browser_delivery;
BEGIN
 IF NOT browser_allowed(w,s,h,loc) OR octet_length(ticket)<>32 THEN RETURN 404; END IF;
 SELECT * INTO x FROM browser_delivery WHERE (workspace_id,source_id,id,actor)=(w,s,d,access_actor()) FOR UPDATE;
 IF NOT FOUND THEN RETURN 404; END IF;
 IF (x.checksum,x.fingerprint,x.fmt,x.locations) IS DISTINCT FROM (h,fp,f,loc) THEN RETURN 409; END IF;
 IF x.snapshot_id IS NOT NULL THEN RETURN 200; END IF;
 IF x.expires_at<=clock_timestamp() THEN RETURN 404; END IF;
 UPDATE browser_delivery SET approval_hash=ticket,approved_until=least(x.expires_at,clock_timestamp()+interval '2 minutes'),
 approval_session=sha256(convert_to(current_setting('rs.session_proof'),'UTF8')) WHERE (workspace_id,id)=(w,d);
 RETURN 202;
END $$;
REVOKE ALL ON FUNCTION browser_approve(uuid,uuid,uuid,text,text,text,uuid[],bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_approve(uuid,uuid,uuid,text,text,text,uuid[],bytea) TO rs_runtime;

CREATE FUNCTION browser_actor(w uuid,s uuid,place text DEFAULT NULL) RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT d.actor FROM browser_delivery d JOIN access_session ses ON ses.token_hash=d.approval_session AND ses.principal_id=d.actor
 JOIN principal p ON p.id=d.actor JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(w,d.actor)
 JOIN workspace ws ON ws.id=w JOIN source src ON (src.workspace_id,src.id)=(w,s)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,s,d.checksum)
 WHERE session_user='rs_upload' AND retention_online() AND (d.workspace_id,d.source_id)=(w,s)
 AND d.id=nullif(current_setting('rs.upload_id',true),'')::uuid
 AND d.approval_hash=sha256(convert_to(nullif(current_setting('rs.upload_proof',true),''),'UTF8'))
 AND d.approved_until>statement_timestamp() AND d.expires_at>statement_timestamp() AND d.snapshot_id IS NULL
 AND ses.revoked_at IS NULL AND ses.expires_at>statement_timestamp() AND p.state='active' AND m.state='active'
 AND m.role IN ('owner','analyst') AND ws.state='active' AND src.kind='synthetic_demo'
 AND (place IS NULL OR EXISTS(SELECT FROM source_location l WHERE (l.workspace_id,l.source_id,l.external_place_id)=(w,s,place)
 AND l.location_id=ANY(d.locations)))
$$;
CREATE FUNCTION browser_scope(w uuid,s uuid,place text DEFAULT NULL) RETURNS boolean LANGUAGE sql STABLE
 SET search_path=pg_catalog,public,pg_temp AS $$ SELECT browser_actor(w,s,place) IS NOT NULL $$;
CREATE FUNCTION browser_claim(w uuid,s uuid,d uuid,h text,fp text)
 RETURNS TABLE(root text,source_key text,principal_id uuid,fmt text,locations uuid[]) LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 RETURN QUERY SELECT ws.delivery_object_root::text,coalesce(src.external_source_id,src.source_key)::text,
 x.actor,x.fmt,x.locations FROM browser_delivery x JOIN workspace ws ON ws.id=x.workspace_id
 JOIN source src ON (src.workspace_id,src.id)=(w,s) JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,s,h)
 JOIN workspace_membership m ON (m.workspace_id,m.principal_id)=(w,x.actor)
 JOIN access_session ses ON (ses.token_hash,ses.principal_id)=(x.approval_session,x.actor)
 JOIN principal p ON p.id=x.actor WHERE (x.workspace_id,x.source_id,x.id,x.checksum,x.fingerprint)=(w,s,d,h,fp)
 AND x.actor=browser_actor(w,s)
 FOR UPDATE OF src,x FOR SHARE OF ws,m,ses,p,a;
END $$;
REVOKE ALL ON FUNCTION browser_actor(uuid,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_actor(uuid,uuid,text) TO rs_upload;
CREATE FUNCTION browser_complete(w uuid,s uuid,d uuid,snap uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT browser_scope(w,s) OR d IS DISTINCT FROM nullif(current_setting('rs.upload_id',true),'')::uuid
 OR NOT EXISTS(SELECT FROM snapshot WHERE (workspace_id,source_id,batch_id,id)=(w,s,d,snap)) THEN RETURN false; END IF;
 UPDATE browser_delivery SET snapshot_id=snap,approval_hash=NULL,approval_session=NULL,approved_until=NULL WHERE (workspace_id,source_id,id,actor)=(w,s,d,browser_actor(w,s))
 AND (snapshot_id IS NULL OR snapshot_id=snap);
 RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION browser_claim(uuid,uuid,uuid,text,text),browser_scope(uuid,uuid,text),browser_complete(uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO rs_upload;
GRANT SELECT(version_num) ON alembic_version TO rs_upload;
GRANT EXECUTE ON FUNCTION retention_stamp(),browser_claim(uuid,uuid,uuid,text,text),browser_scope(uuid,uuid,text),
 browser_complete(uuid,uuid,uuid,uuid) TO rs_upload;
CREATE POLICY upload_locations ON source_location FOR SELECT TO rs_upload USING(browser_scope(workspace_id,source_id,external_place_id));
GRANT SELECT(workspace_id,source_id,external_place_id) ON source_location TO rs_upload;
CREATE POLICY upload_identity_read ON review_identity FOR SELECT TO rs_upload USING(browser_scope(workspace_id,source_id,external_place_id));
CREATE POLICY upload_identity_append ON review_identity FOR INSERT TO rs_upload WITH CHECK(browser_scope(workspace_id,source_id,external_place_id)
 AND (observation_batch_id IS NULL OR observation_batch_id=current_setting('rs.upload_id')::uuid));
CREATE POLICY upload_version_read ON review_version FOR SELECT TO rs_upload USING(browser_scope(workspace_id,source_id,external_place_id));
CREATE POLICY upload_version_append ON review_version FOR INSERT TO rs_upload WITH CHECK(browser_scope(workspace_id,source_id,external_place_id)
 AND batch_id=current_setting('rs.upload_id')::uuid);
CREATE FUNCTION browser_batch_matches(w uuid,s uuid,d uuid,h text,receipt jsonb) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp AS $$ SELECT EXISTS(SELECT FROM browser_delivery x
 WHERE (x.workspace_id,x.source_id,x.id,x.checksum,x.fingerprint,x.actor)=(w,s,d,h,receipt->>'fingerprint',browser_actor(w,s))) $$;
REVOKE ALL ON FUNCTION browser_batch_matches(uuid,uuid,uuid,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_batch_matches(uuid,uuid,uuid,text,jsonb) TO rs_upload;
CREATE POLICY upload_batch ON import_batch FOR ALL TO rs_upload
 USING(browser_scope(workspace_id,source_id) AND id=current_setting('rs.upload_id',true)::uuid)
 WITH CHECK(browser_scope(workspace_id,source_id) AND id=current_setting('rs.upload_id',true)::uuid
 AND registered_by=browser_actor(workspace_id,source_id) AND browser_batch_matches(workspace_id,source_id,id,file_sha256,delivery));

CREATE POLICY upload_position ON import_row FOR ALL TO rs_upload USING(browser_scope(workspace_id,source_id) AND batch_id=current_setting('rs.upload_id',true)::uuid);
CREATE POLICY upload_snapshot ON snapshot FOR ALL TO rs_upload USING(browser_scope(workspace_id,source_id) AND batch_id=current_setting('rs.upload_id',true)::uuid);
CREATE POLICY upload_member ON snapshot_member FOR ALL TO rs_upload USING(browser_scope(workspace_id,source_id) AND EXISTS
 (SELECT FROM snapshot x WHERE (x.workspace_id,x.source_id,x.id)=(snapshot_member.workspace_id,snapshot_member.source_id,snapshot_member.snapshot_id)));
GRANT SELECT,INSERT ON import_batch,review_identity,review_version,import_row,snapshot,snapshot_member TO rs_upload;
CREATE FUNCTION browser_role_safe() RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT rolcanlogin AND NOT (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication)
 AND NOT EXISTS(SELECT FROM pg_auth_members WHERE member=r.oid)
 AND NOT has_schema_privilege(r.oid,'public','CREATE') AND NOT has_database_privilege(r.oid,current_database(),'CREATE,TEMP')
 AND NOT EXISTS(SELECT FROM pg_class c WHERE c.relowner=r.oid OR (c.relnamespace='public'::regnamespace
 AND c.relkind='r' AND (has_table_privilege(r.oid,c.oid,'DELETE,TRUNCATE') OR has_any_column_privilege(r.oid,c.oid,'UPDATE'))))
 AND NOT EXISTS(SELECT FROM pg_class WHERE relnamespace='public'::regnamespace AND
 relname=ANY(ARRAY['review_version','review_identity','import_row','import_batch','snapshot','snapshot_member','source_location',
 'browser_admission','browser_delivery','browser_summary']) AND NOT relrowsecurity)
 AND NOT EXISTS(SELECT FROM pg_class c WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','v')
 AND ((c.relname NOT IN ('alembic_version','import_batch','review_identity','review_version','import_row','snapshot','snapshot_member','source_location')
 AND has_any_column_privilege(r.oid,c.oid,'SELECT')) OR (c.relname NOT IN ('import_batch','review_identity','review_version','import_row','snapshot','snapshot_member')
 AND has_any_column_privilege(r.oid,c.oid,'INSERT'))))
 FROM pg_roles r WHERE rolname='rs_upload'
$$;
REVOKE ALL ON FUNCTION browser_role_safe() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION browser_role_safe() TO rs_upload;

-- Keep the verified producer's numeric projection with its immutable artifact.
CREATE FUNCTION browser_project_summary() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE raw text := nullif(current_setting('rs.summary_projection',true),''); doc jsonb; item jsonb;
 name text; group_name text; projected jsonb := '{}'; group_values jsonb;
BEGIN
 IF raw IS NULL THEN RETURN NEW; END IF;
 IF session_user<>'rs_worker' OR octet_length(raw)>65536 OR encode(sha256(convert_to(raw,'UTF8')),'hex')<>NEW.checksum
 THEN RAISE EXCEPTION 'summary_projection_invalid'; END IF;
 doc:=raw::jsonb;
 IF doc->>'schema' IS DISTINCT FROM 'snapshot-summary-1' OR doc->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
 OR doc->>'run_id' IS DISTINCT FROM NEW.run_id::text THEN RAISE EXCEPTION 'summary_projection_invalid'; END IF;
 FOREACH group_name IN ARRAY ARRAY['counts','eligibility','rating_distribution'] LOOP
  group_values:='{}';
  FOREACH name IN ARRAY CASE group_name WHEN 'counts' THEN ARRAY['observations','quarantined','confirmed_duplicates']
   WHEN 'eligibility' THEN ARRAY['unknown_author','usable_text','known_rating','comparable_date']
   ELSE ARRAY['1','2','3','4','5'] END LOOP
   item:=CASE WHEN group_name='counts' THEN doc->name ELSE doc->group_name->name END;
   IF group_name='rating_distribution' AND doc->group_name='null'::jsonb THEN group_values:='null'::jsonb; EXIT; END IF;
   IF jsonb_typeof(item->'numerator') IS DISTINCT FROM 'number' OR jsonb_typeof(item->'denominator') IS DISTINCT FROM 'number'
    OR (item->>'numerator') !~ '^[0-9]{1,5}$' OR (item->>'denominator') !~ '^[0-9]{1,5}$'
    OR (item->>'numerator')::int>(item->>'denominator')::int THEN RAISE EXCEPTION 'summary_projection_invalid'; END IF;
   group_values:=group_values||jsonb_build_object(name,jsonb_build_object('numerator',(item->>'numerator')::int,'denominator',(item->>'denominator')::int));
  END LOOP;
  projected:=projected||jsonb_build_object(group_name,group_values);
 END LOOP;
 INSERT INTO browser_summary VALUES(NEW.workspace_id,NEW.run_id,NEW.id,projected);
 RETURN NEW;
END $$;
CREATE TRIGGER browser_project_summary AFTER INSERT ON artifact FOR EACH ROW EXECUTE FUNCTION browser_project_summary();
REVOKE ALL ON FUNCTION browser_project_summary() FROM PUBLIC;


CREATE POLICY scoped_export ON browser_delivery FOR SELECT TO rs_retention USING
 ((workspace_id,source_id) IN (SELECT * FROM retention_export_sources()));
GRANT SELECT(workspace_id,id,source_id,fmt,locations,checksum,fingerprint,diagnostics,expires_at,snapshot_id)
 ON browser_delivery TO rs_retention;
