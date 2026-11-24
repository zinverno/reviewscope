-- The row already binds a synthetic file checksum to a workspace and source and is access-sealed.
ALTER TABLE browser_admission ADD COLUMN author_context_allowed boolean NOT NULL DEFAULT false;

CREATE FUNCTION reviewer_context_page(w uuid, src uuid, snap uuid, anchor uuid, off_n integer, page_n integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE ctx record; author_key text; result jsonb;
BEGIN
 IF session_user <> 'rs_runtime' OR current_setting('rs.workspace',true) IS DISTINCT FROM w::text
 OR off_n IS NULL OR off_n NOT BETWEEN 0 AND 10000 OR page_n IS NULL OR page_n NOT BETWEEN 1 AND 50
 OR NOT coalesce(retention_online(),false) THEN RETURN NULL; END IF;

 -- Hold the same byte grant, session and membership through the bounded read and commit.
 SELECT s.batch_id,s.manifest_sha256,a.author_context_allowed INTO ctx FROM snapshot s
 JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,src,s.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,src,b.file_sha256)
 JOIN source src_row ON (src_row.workspace_id,src_row.id)=(w,src)
 JOIN workspace ws ON ws.id=w
 JOIN workspace_membership m ON m.workspace_id=w
 JOIN principal p ON p.id=m.principal_id
 JOIN access_session x ON x.principal_id=p.id
 WHERE (s.workspace_id,s.source_id,s.id)=(w,src,snap)
 AND s.filters->>'format'='canonical-json-v1' AND s.filters->>'selection'='accepted_in_delivery'
 AND b.procedure_version='canonical-preview-1' AND b.status='accepted' AND b.input_count BETWEEN 1 AND 10000
 AND a.evidence_allowed AND src_row.kind='synthetic_demo' AND ws.state='active'
 AND m.role IN ('owner','analyst') AND m.state='active' AND p.state='active'
 AND x.token_hash=sha256(convert_to(nullif(current_setting('rs.session_proof',true),''),'UTF8'))
 AND x.revoked_at IS NULL AND x.expires_at>statement_timestamp()
 FOR SHARE OF a,src_row,ws,m,p,x;
 IF NOT FOUND THEN RETURN NULL; END IF;

 SELECT v.source_reviewer_id INTO author_key FROM review_version v
 JOIN snapshot_member sm ON (sm.workspace_id,sm.source_id,sm.snapshot_id,sm.version_id)=(w,src,snap,v.id)
 JOIN import_row r ON (r.workspace_id,r.source_id,r.batch_id,r.version_id)=(w,src,ctx.batch_id,v.id)
 WHERE (v.workspace_id,v.source_id,v.id)=(w,src,anchor)
 AND r.status='accepted';
 IF NOT FOUND THEN RETURN NULL; END IF;
 -- An incompatible member invalidates the method for the entire pinned selection.
 IF EXISTS (
  SELECT FROM snapshot_member sm JOIN review_version member
   ON (member.workspace_id,member.source_id,member.id)=(sm.workspace_id,sm.source_id,sm.version_id)
  WHERE (sm.workspace_id,sm.source_id,sm.snapshot_id)=(w,src,snap)
  AND member.normalization_version<>'canonical-preview-1'
 ) THEN RETURN NULL; END IF;
 result:=jsonb_build_object('method','snapshot-reviewer-context-v1','workspace_id',w,'source_id',src,
 'snapshot_id',snap,'anchor_version_id',anchor,'manifest_sha256',ctx.manifest_sha256,
 'offset',off_n,'limit',page_n);
 IF author_key IS NULL OR NOT ctx.author_context_allowed THEN
   RETURN result || jsonb_build_object('status',CASE WHEN author_key IS NULL THEN 'NO_REVIEWER_ID'
   ELSE 'AUTHOR_CONTEXT_NOT_ADMITTED' END,'total',NULL,'metrics',NULL,'rows','[]'::jsonb);
 END IF;

 WITH history AS MATERIALIZED (
  SELECT v.id AS version_id,r.position,sl.external_place_id,sl.location_id,left(l.name,160) AS location_name,
   CASE WHEN v.metadata->>'event_basis' IN ('published','submitted')
     THEN coalesce(v.event_date,(v.event_at AT TIME ZONE 'UTC')::date) END AS day,
   v.rating,NOT ('missing_text'=ANY(r.warnings)) AS has_text
  FROM snapshot_member sm
  JOIN import_row r ON (r.workspace_id,r.source_id,r.batch_id,r.version_id)=(w,src,ctx.batch_id,sm.version_id)
  JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(w,src,sm.version_id)
  JOIN source_location sl ON (sl.workspace_id,sl.source_id,sl.external_place_id)=(w,src,v.external_place_id)
  JOIN location l ON (l.workspace_id,l.project_id,l.id)=(w,sl.project_id,sl.location_id)
  WHERE (sm.workspace_id,sm.source_id,sm.snapshot_id)=(w,src,snap)
  AND r.status='accepted' AND v.source_reviewer_id COLLATE "C"=author_key COLLATE "C"
 ), page AS (
  SELECT * FROM history ORDER BY day DESC NULLS LAST,position,version_id OFFSET off_n LIMIT page_n
 ), ratings AS (SELECT rating,count(*) AS n FROM history WHERE rating IS NOT NULL GROUP BY rating)
 SELECT result || jsonb_build_object('status','computed','total',count(*),
  'metrics',jsonb_build_object('places',count(DISTINCT external_place_id),'dated',count(day),'first_day',min(day),
  'last_day',max(day),'rated',count(rating),'rating_mean',avg(rating),'rating_counts',
  coalesce((SELECT jsonb_object_agg(rating,n) FROM ratings),'{}'::jsonb),
  'with_text',count(*) FILTER(WHERE has_text)),
  'rows',coalesce((SELECT jsonb_agg((to_jsonb(p)-'external_place_id') || jsonb_build_object('body',CASE WHEN p.has_text THEN v.text_content END)
  ORDER BY p.day DESC NULLS LAST,p.position,p.version_id)
  FROM page p JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(w,src,p.version_id)),'[]'::jsonb))
 INTO result FROM history;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION reviewer_context_page(uuid,uuid,uuid,uuid,integer,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reviewer_context_page(uuid,uuid,uuid,uuid,integer,integer) TO rs_runtime;
