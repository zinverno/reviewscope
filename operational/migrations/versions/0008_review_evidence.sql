-- Intake approval and permission to display canonical text are separate operator decisions.
ALTER TABLE browser_admission ADD COLUMN evidence_allowed boolean NOT NULL DEFAULT false,
 ADD COLUMN evidence_link_hashes sha256_hex[] NOT NULL DEFAULT '{}' CHECK (
 cardinality(evidence_link_hashes)<=64 AND coalesce(array_ndims(evidence_link_hashes),1)=1
 AND array_position(evidence_link_hashes,NULL) IS NULL);

CREATE FUNCTION review_evidence_page(w uuid,k uuid,vkey uuid,source_filter uuid,place_filter uuid,
 stars integer,with_text boolean,with_date boolean,first_day date,last_day date,pos integer,
 page_offset integer,page_size integer) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp SET TimeZone='UTC' AS $$
DECLARE context record; result jsonb;
BEGIN
 IF session_user<>'rs_runtime' OR current_setting('rs.workspace',true) IS DISTINCT FROM w::text
 OR page_offset IS NULL OR page_offset NOT BETWEEN 0 AND 10000 OR page_size IS NULL OR page_size NOT BETWEEN 1 AND 50
 OR stars NOT BETWEEN 1 AND 5 OR pos NOT BETWEEN 1 AND 10000 OR first_day>last_day THEN RETURN NULL; END IF;
 SELECT s.source_id,s.batch_id,s.created_at,b.coverage,b.covered_from,b.covered_until,src.origin_label,a.evidence_link_hashes
 INTO context FROM snapshot s JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(w,s.source_id,s.batch_id)
 JOIN source src ON (src.workspace_id,src.id)=(w,s.source_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,s.source_id,b.file_sha256)
 WHERE (s.workspace_id,s.id)=(w,k) AND s.filters->>'format'='canonical-json-v1'
 AND a.evidence_allowed AND browser_member(w,s.source_id);
 IF NOT FOUND THEN RETURN NULL; END IF;

 WITH matched AS MATERIALIZED (
  SELECT r.position,v.id FROM import_row r JOIN snapshot_member sm
    ON (sm.workspace_id,sm.source_id,sm.snapshot_id,sm.version_id)=(w,context.source_id,k,r.version_id)
   JOIN review_version v ON (v.workspace_id,v.source_id,v.identity_id,v.id)=(w,context.source_id,r.identity_id,r.version_id)
   JOIN source_location sl ON (sl.workspace_id,sl.source_id,sl.external_place_id)=(w,context.source_id,v.external_place_id)
  WHERE (r.workspace_id,r.source_id,r.batch_id)=(w,context.source_id,context.batch_id) AND r.status='accepted'
    AND (source_filter IS NULL OR context.source_id=source_filter) AND (place_filter IS NULL OR sl.location_id=place_filter)
    AND (stars IS NULL OR v.rating=stars) AND (pos IS NULL OR r.position=pos)
    AND (with_text IS NULL OR with_text=(coalesce(btrim(v.text_content),'')<>''))
    AND (with_date IS NULL OR with_date=(v.event_date IS NOT NULL OR v.event_at IS NOT NULL))
    AND (first_day IS NULL OR coalesce(v.event_date,(v.event_at AT TIME ZONE 'UTC')::date)>=first_day)
    AND (last_day IS NULL OR coalesce(v.event_date,(v.event_at AT TIME ZONE 'UTC')::date)<=last_day)
    AND (vkey IS NULL OR v.id=vkey)
 ), page AS (SELECT * FROM matched ORDER BY position,id OFFSET page_offset LIMIT page_size),
 details AS (
  SELECT p.position,v.id AS version_id,v.identity_id,i.external_review_id AS review_id,v.version_no,
   v.batch_id AS version_batch_id,sl.location_id,l.name AS location_name,i.external_place_id AS place_key,
   v.rating,v.metadata->'rating_raw' AS raw_rating,v.metadata->'rating_scale' AS rating_scale,
   v.metadata->'event'->>'kind' AS time_kind,v.metadata->'event'->>'value' AS event_value,
   v.metadata->'event'->>'precision' AS time_precision,v.metadata->'event'->>'offset' AS utc_offset,
   v.metadata->>'event_basis' AS event_basis,
   left(v.text_content,160) AS excerpt,
   CASE WHEN vkey IS NOT NULL AND octet_length(v.metadata->>'source_url')<=2048
    AND (v.metadata->>'source_url') COLLATE "C" ~ '^https://([A-Za-z0-9-]+[.])+[A-Za-z]{2,63}(/[A-Za-z0-9/._~-]*)?$'
    AND split_part(v.metadata->>'source_url','://',2) NOT LIKE '%..%'
    AND encode(sha256(convert_to(v.metadata->>'source_url','UTF8')),'hex')=ANY(context.evidence_link_hashes)
    THEN v.metadata->>'source_url' END AS source_url,
   CASE WHEN vkey IS NOT NULL THEN v.text_content END AS full_text
   FROM page p JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(w,context.source_id,p.id)
   JOIN review_identity i ON (i.workspace_id,i.source_id,i.id)=(w,context.source_id,v.identity_id)
   JOIN source_location sl ON (sl.workspace_id,sl.source_id,sl.external_place_id)=(w,context.source_id,i.external_place_id)
   JOIN location l ON (l.workspace_id,l.project_id,l.id)=(w,sl.project_id,sl.location_id)
 )
 SELECT jsonb_build_object('snapshot_id',k,'source_id',context.source_id,'source_label',context.origin_label,
 'batch_id',context.batch_id,'coverage',coalesce(context.coverage::text,'unknown'),
 'covered_from',context.covered_from,'covered_until',context.covered_until,
 'historical',EXISTS(SELECT FROM snapshot newer JOIN import_batch b ON
 (b.workspace_id,b.source_id,b.id)=(newer.workspace_id,newer.source_id,newer.batch_id)
 JOIN browser_admission a ON (a.workspace_id,a.source_id,a.checksum)=(w,context.source_id,b.file_sha256)
 WHERE newer.workspace_id=w AND newer.source_id=context.source_id AND newer.filters->>'format'='canonical-json-v1'
 AND a.evidence_allowed AND (newer.created_at,newer.id)>(context.created_at,k)),
 'total',(SELECT count(*) FROM matched),'offset',page_offset,'limit',page_size,
 'rows',coalesce((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.position,d.version_id) FROM details d),'[]'::jsonb))
 INTO result;
 IF vkey IS NOT NULL AND result->'rows'='[]'::jsonb THEN RETURN NULL; END IF;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION review_evidence_page(uuid,uuid,uuid,uuid,uuid,integer,boolean,boolean,date,date,integer,integer,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION review_evidence_page(uuid,uuid,uuid,uuid,uuid,integer,boolean,boolean,date,date,integer,integer,integer) TO rs_runtime;
