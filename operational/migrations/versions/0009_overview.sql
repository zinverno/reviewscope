-- Pre-existing artifacts retain their numeric projection; absent context stays NULL.
ALTER TABLE browser_summary ADD COLUMN context jsonb;

CREATE FUNCTION browser_summary_context() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,public,pg_temp SET TimeZone='UTC' AS $$
DECLARE doc jsonb := current_setting('rs.summary_projection')::jsonb;
 scope record; section text; reasons jsonb; code text; item jsonb; n integer; d integer;
BEGIN
 -- This row is inserted only by the existing checksum-verified artifact trigger.
 SELECT sn.id,sn.source_id,sn.batch_id,b.coverage,b.covered_from,b.covered_until
 INTO STRICT scope FROM analysis_run r JOIN snapshot sn ON (sn.workspace_id,sn.id)=(r.workspace_id,r.snapshot_id)
 JOIN import_batch b ON (b.workspace_id,b.source_id,b.id)=(sn.workspace_id,sn.source_id,sn.batch_id)
 WHERE (r.workspace_id,r.id)=(NEW.workspace_id,NEW.run_id) AND r.job_kind='snapshot_integrity_and_summary_v1';
 IF doc->>'snapshot_id' IS DISTINCT FROM scope.id::text OR doc->>'source_id' IS DISTINCT FROM scope.source_id::text
 OR doc->>'batch_id' IS DISTINCT FROM scope.batch_id::text OR doc#>>'{parameters,selection}' IS DISTINCT FROM 'accepted_in_delivery'
 OR doc->>'coverage' IS DISTINCT FROM scope.coverage::text
 OR (doc->>'covered_from')::timestamptz IS DISTINCT FROM scope.covered_from
 OR (doc->>'covered_until')::timestamptz IS DISTINCT FROM scope.covered_until
 THEN RAISE EXCEPTION 'summary_projection_invalid'; END IF;
 NEW.context:=jsonb_build_object('source_id',scope.source_id,'batch_id',scope.batch_id,'coverage',scope.coverage,
 'covered_from',scope.covered_from,'covered_until',scope.covered_until,'selection','accepted_in_delivery');
 FOREACH section IN ARRAY ARRAY['delivery_exclusions','metric_exclusions'] LOOP
  IF jsonb_typeof(doc->section) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'summary_projection_invalid'; END IF;
  reasons:='{}';
  FOR code,item IN SELECT * FROM jsonb_each(doc->section) LOOP
   IF code <> ALL(ARRAY['empty_input','over_limit','invalid_encoding','invalid_csv','invalid_json','invalid_shape','duplicate_key',
     'duplicate_header','invalid_header','invalid_mapping','conflicting_mapping','unknown_column','unexpected_column',
     'invalid_passport','invalid_format','private_output','io_error','invalid_arguments','invalid_json_item','invalid_field_type','row_width','missing_source','missing_place',
     'binding_mismatch','observation_mismatch','conflicting_id','confirmed_duplicate','missing_review_id',
     'missing_reviewer','missing_text','missing_date','invalid_date','unknown_timezone','ambiguous_time',
     'nonexistent_time','ineligible_event_basis','missing_rating','invalid_rating','unmapped_scale','invalid_url',
     'date_local','date_invalid','date_missing']) THEN RAISE EXCEPTION 'summary_projection_invalid'; END IF;
   n:=(item->>'numerator')::integer; d:=(item->>'denominator')::integer;
   IF n IS NULL OR d IS NULL OR n<0 OR n>d OR d>10000 OR d IS DISTINCT FROM
   (NEW.metrics#>> CASE section WHEN 'delivery_exclusions' THEN '{counts,observations,denominator}'::text[]
   ELSE '{counts,observations,numerator}'::text[] END)::integer THEN RAISE EXCEPTION 'summary_projection_invalid'; END IF;
   reasons:=reasons || jsonb_build_object(code,jsonb_build_object('numerator',n,'denominator',d));
  END LOOP;
  NEW.context:=NEW.context || jsonb_build_object(section,reasons);
 END LOOP;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION browser_summary_context() FROM PUBLIC;
CREATE TRIGGER project_summary_context BEFORE INSERT ON browser_summary
 FOR EACH ROW EXECUTE FUNCTION browser_summary_context();
