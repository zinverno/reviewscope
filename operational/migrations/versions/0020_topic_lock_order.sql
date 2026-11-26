-- Check the immutable target without row locks before entering the PR-20 writer order.
CREATE FUNCTION topic_confirm_writer(w uuid, src uuid, s uuid, res uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE saved topic_result;
BEGIN
 IF session_user <> 'rs_runtime' OR w IS DISTINCT FROM nullif(current_setting('rs.workspace',true),'')::uuid
 OR browser_member(w,src) IS NOT TRUE OR topic_admitted(w,src,s) IS NOT TRUE THEN RETURN false; END IF;
 SELECT * INTO saved FROM topic_result WHERE (workspace_id,source_id,snapshot_id,result_id)=(w,src,s,res);
 IF NOT FOUND THEN RETURN false; END IF;
 BEGIN
  IF job_writer(w) IS DISTINCT FROM 200 THEN RAISE insufficient_privilege; END IF;
  IF topic_scope(w,src,s,saved.decision_id,saved.human_id) IS NOT TRUE THEN RAISE insufficient_privilege; END IF;
  IF aspect_scope(w,saved.project_id) IS NOT TRUE THEN RAISE insufficient_privilege; END IF;
  RETURN true;
 -- A denied preflight releases its locks even if the caller keeps its transaction open.
 EXCEPTION WHEN insufficient_privilege THEN RETURN false;
 END;
END $$;
REVOKE ALL ON FUNCTION topic_confirm_writer(uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION topic_confirm_writer(uuid,uuid,uuid,uuid) TO rs_runtime;

-- Keep the 0019 validation, idempotency, CAS and final authority check in one implementation.
ALTER FUNCTION topic_review(uuid,uuid,uuid,uuid,uuid,text,uuid[],text,jsonb,uuid,uuid,jsonb)
 RENAME TO topic_apply_review;
REVOKE ALL ON FUNCTION topic_apply_review(uuid,uuid,uuid,uuid,uuid,text,uuid[],text,jsonb,uuid,uuid,jsonb)
 FROM PUBLIC,rs_runtime;

CREATE FUNCTION topic_review(w uuid,src uuid,s uuid,res uuid,k uuid,act text,parents uuid[],note text,
 parts jsonb,a uuid,v uuid,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF act='confirm' AND payload IS NOT NULL THEN
  IF NOT topic_confirm_writer(w,src,s,res) THEN RETURN NULL; END IF;
 END IF;
 RETURN topic_apply_review(w,src,s,res,k,act,parents,note,parts,a,v,payload);
END $$;
REVOKE ALL ON FUNCTION topic_review(uuid,uuid,uuid,uuid,uuid,text,uuid[],text,jsonb,uuid,uuid,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION topic_review(uuid,uuid,uuid,uuid,uuid,text,uuid[],text,jsonb,uuid,uuid,jsonb) TO rs_runtime;
