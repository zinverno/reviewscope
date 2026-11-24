CREATE OR REPLACE FUNCTION reviewer_context_page(w uuid, src uuid, snap uuid, anchor uuid, off_n integer, page_n integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE ctx record; result jsonb;
BEGIN
 IF session_user <> 'rs_runtime' OR current_setting('rs.workspace',true) IS DISTINCT FROM w::text
 OR off_n IS NULL OR off_n NOT BETWEEN 0 AND 10000 OR page_n IS NULL OR page_n NOT BETWEEN 1 AND 50
 OR NOT coalesce(retention_online(),false) THEN RETURN NULL; END IF;

 -- Hold the same byte grant, session and membership through the bounded read and commit.
 SELECT s.batch_id,s.manifest_sha256,s.version_ids,a.author_context_allowed INTO ctx FROM snapshot s
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

 -- Check the independent member seal before resolving the accepted receipt.
 IF ctx.version_ids IS DISTINCT FROM ARRAY(
  SELECT sm.version_id FROM snapshot_member sm
  WHERE (sm.workspace_id,sm.source_id,sm.snapshot_id)=(w,src,snap) ORDER BY sm.version_id
 ) OR anchor IS NULL OR NOT (anchor=ANY(ctx.version_ids)) THEN RETURN NULL; END IF;
 result:=jsonb_build_object('method','snapshot-reviewer-context-v1','workspace_id',w,'source_id',src,
 'snapshot_id',snap,'anchor_version_id',anchor,'manifest_sha256',ctx.manifest_sha256,
 'offset',off_n,'limit',page_n);

 -- A full equijoin exposes missing records on either side and admits a hash or merge plan.
 -- Resolve the bounded version set once. Identity and normalization apply to every receipt row.
 WITH resolved AS MATERIALIZED (
  SELECT r.version_id,r.position,v.source_reviewer_id,v.external_place_id,
   r.identity_id=v.identity_id AND v.normalization_version='canonical-preview-1' AS compatible,
   CASE WHEN v.metadata->>'event_basis' IN ('published','submitted')
     THEN coalesce(v.event_date,(v.event_at AT TIME ZONE 'UTC')::date) END AS day,
   v.rating,NOT ('missing_text'=ANY(r.warnings)) AS has_text
  FROM (SELECT version_id,position,identity_id,warnings FROM import_row
        WHERE (workspace_id,source_id,batch_id)=(w,src,ctx.batch_id) AND status='accepted') r
  FULL JOIN (SELECT id,identity_id,normalization_version,source_reviewer_id,external_place_id,
                    metadata,event_date,event_at,rating FROM review_version
             WHERE (workspace_id,source_id)=(w,src) AND id=ANY(ctx.version_ids)) v
  ON v.id=r.version_id
 ), checked AS (
  SELECT array_agg(CASE WHEN compatible THEN version_id END ORDER BY version_id)=ctx.version_ids AS valid,
   max(source_reviewer_id) FILTER (WHERE version_id=anchor) AS author_key
  FROM resolved
 ), history AS MATERIALIZED (
  SELECT r.version_id,r.position,r.external_place_id,r.day,r.rating,r.has_text
  FROM resolved r WHERE ctx.author_context_allowed
   AND r.source_reviewer_id COLLATE "C"=(SELECT author_key FROM checked) COLLATE "C"
 ), page AS MATERIALIZED (
  SELECT * FROM history ORDER BY day DESC NULLS LAST,position,version_id OFFSET off_n LIMIT page_n
 ), bodies AS MATERIALIZED (
  SELECT id,text_content FROM review_version WHERE (workspace_id,source_id)=(w,src)
   AND id=ANY(ARRAY(SELECT version_id FROM page WHERE has_text))
 ), ratings AS (SELECT rating,count(*) AS n FROM history WHERE rating IS NOT NULL GROUP BY rating)
 SELECT CASE WHEN checked.valid IS NOT TRUE THEN NULL
 WHEN checked.author_key IS NULL OR NOT ctx.author_context_allowed THEN
  result || jsonb_build_object('status',CASE WHEN checked.author_key IS NULL THEN 'NO_REVIEWER_ID'
   ELSE 'AUTHOR_CONTEXT_NOT_ADMITTED' END,'total',NULL,'metrics',NULL,'rows','[]'::jsonb)
 ELSE (SELECT result || jsonb_build_object('status','computed','total',count(*),
  'metrics',jsonb_build_object('places',count(DISTINCT external_place_id),'dated',count(day),'first_day',min(day),
  'last_day',max(day),'rated',count(rating),'rating_mean',avg(rating),'rating_counts',
  coalesce((SELECT jsonb_object_agg(rating,n) FROM ratings),'{}'::jsonb),
  'with_text',count(*) FILTER(WHERE has_text)),
  'rows',coalesce((SELECT jsonb_agg((to_jsonb(p)-'external_place_id') || jsonb_build_object(
  'location_id',sl.location_id,'location_name',left(l.name,160),'body',b.text_content)
  ORDER BY p.day DESC NULLS LAST,p.position,p.version_id)
  FROM page p JOIN source_location sl ON (sl.workspace_id,sl.source_id,sl.external_place_id)=(w,src,p.external_place_id)
  JOIN location l ON (l.workspace_id,l.project_id,l.id)=(w,sl.project_id,sl.location_id)
  LEFT JOIN bodies b ON b.id=p.version_id),'[]'::jsonb)) FROM history)
 END INTO result FROM checked;
 RETURN result;
END $$;
