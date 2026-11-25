-- A catalog belongs to a project across deliveries. Definitions are append-only.
CREATE FUNCTION aspect_nonblank(value text) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT btrim(value,U&'\0009\000a\000b\000c\000d\001c\001d\001e\001f\0020\0085\00a0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200a\2028\2029\202f\205f\3000')<>''
$$;
CREATE FUNCTION aspect_payload_valid(d jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF jsonb_typeof(d)<>'object' OR octet_length(d::text)>16384
 OR NOT d ?& ARRAY['name','definition','include','exclude']
 OR d - ARRAY['name','definition','include','exclude'] <> '{}'
 OR jsonb_typeof(d->'name')<>'string' OR length(d->>'name') NOT BETWEEN 1 AND 120
 OR jsonb_typeof(d->'definition')<>'string' OR length(d->>'definition') NOT BETWEEN 1 AND 2000
 OR NOT aspect_nonblank(d->>'name') OR NOT aspect_nonblank(d->>'definition')
 OR jsonb_typeof(d->'include')<>'array' OR jsonb_typeof(d->'exclude')<>'array' THEN RETURN false; END IF;
 RETURN jsonb_array_length(d->'include') BETWEEN 1 AND 10 AND jsonb_array_length(d->'exclude') BETWEEN 1 AND 10
 AND NOT EXISTS(SELECT FROM jsonb_array_elements((d->'include')||(d->'exclude')) e
 GROUP BY e HAVING count(*)>1 OR jsonb_typeof(e)<>'string' OR length(e#>>'{}')>500 OR NOT aspect_nonblank(e#>>'{}'));
END $$;
CREATE FUNCTION aspect_digest(d jsonb) RETURNS text LANGUAGE sql IMMUTABLE STRICT
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT encode(sha256(convert_to('aspect-definition-1' || chr(10) || d::text,'UTF8')),'hex')
$$;
-- Pin addresses only. The public overview supplies phrases and cluster counts.
CREATE FUNCTION aspect_origin_valid(origin text) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT origin='manual' OR origin IN (
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:1730d216674f5ccdf3a18a9a562b38eb11e6dc9fef89d0ead983b5df15642654:p1:3',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:1730d216674f5ccdf3a18a9a562b38eb11e6dc9fef89d0ead983b5df15642654:p1:1',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:1730d216674f5ccdf3a18a9a562b38eb11e6dc9fef89d0ead983b5df15642654:p1:2',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:1730d216674f5ccdf3a18a9a562b38eb11e6dc9fef89d0ead983b5df15642654:p1:0',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:91e30503ce230d77d0b5a90a4aa0d0f778e8e376339c182293b02e6b7719c984:p2:0',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:91e30503ce230d77d0b5a90a4aa0d0f778e8e376339c182293b02e6b7719c984:p2:1',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:4607c6e0aba126a09657afa6753872977176a83bd593522d7bf6ca57e4aaf2af:p3:2',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:4607c6e0aba126a09657afa6753872977176a83bd593522d7bf6ca57e4aaf2af:p3:0',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:4607c6e0aba126a09657afa6753872977176a83bd593522d7bf6ca57e4aaf2af:p3:1',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:66776c05b6a9d87d69ec5961c86ee28ff6afe2b69bccf8c68d5ecebea5fbd0f0:p4:1',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:66776c05b6a9d87d69ec5961c86ee28ff6afe2b69bccf8c68d5ecebea5fbd0f0:p4:0',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:ec87bde729ef89075c85b58974e8f8e92728693bd7d1d8e7a97ab314b3e10a0d:p5:1',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:ec87bde729ef89075c85b58974e8f8e92728693bd7d1d8e7a97ab314b3e10a0d:p5:0',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:f285d11ff5db3e2777a5f87f0b8e504c764fc6bd5db1fc2c2074377465f5f3dd:p6:1',
 '85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2:9bc1ca105565bad5210235e0505140fa94b52c8cc9ce40720ba90ef7e16cfb11:f285d11ff5db3e2777a5f87f0b8e504c764fc6bd5db1fc2c2074377465f5f3dd:p6:0'
 )
$$;
CREATE TABLE aspect (
 workspace_id uuid NOT NULL, project_id uuid NOT NULL, aspect_id uuid NOT NULL DEFAULT gen_random_uuid(),
 current_no integer NOT NULL CHECK(current_no>0), created_by uuid NOT NULL REFERENCES principal,
 created_at timestamptz NOT NULL DEFAULT now(), archived_at timestamptz, archive_key uuid, archived_by uuid REFERENCES principal,
 CHECK(num_nonnulls(archived_at,archive_key,archived_by) IN (0,3)),
 PRIMARY KEY(workspace_id,project_id,aspect_id), UNIQUE(workspace_id,project_id,archive_key),
 FOREIGN KEY(workspace_id,project_id) REFERENCES client_project ON DELETE CASCADE
);
CREATE TABLE aspect_version (
 workspace_id uuid NOT NULL, project_id uuid NOT NULL, aspect_id uuid NOT NULL, version_no integer NOT NULL CHECK(version_no>0),
 version_id uuid NOT NULL DEFAULT gen_random_uuid(), payload jsonb NOT NULL CHECK(aspect_payload_valid(payload)),
 digest text GENERATED ALWAYS AS (aspect_digest(payload)) STORED, origin text NOT NULL CHECK(aspect_origin_valid(origin)),
 request_key uuid NOT NULL, expected_version uuid, created_by uuid NOT NULL REFERENCES principal, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,project_id,aspect_id,version_no), UNIQUE(workspace_id,project_id,request_key),
 UNIQUE(workspace_id,project_id,aspect_id,version_id,digest), UNIQUE(version_id),
 FOREIGN KEY(workspace_id,project_id,aspect_id) REFERENCES aspect ON DELETE CASCADE,
 CHECK((version_no=1)=(expected_version IS NULL))
);
ALTER TABLE aspect ADD FOREIGN KEY(workspace_id,project_id,aspect_id,current_no) REFERENCES aspect_version
 DEFERRABLE INITIALLY DEFERRED;
CREATE TRIGGER immutable_aspect_version BEFORE UPDATE OR DELETE ON aspect_version FOR EACH ROW EXECUTE FUNCTION reject_history_change();
ALTER TABLE aspect ENABLE ROW LEVEL SECURITY;
ALTER TABLE aspect_version ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON aspect,aspect_version FROM PUBLIC,rs_runtime,rs_worker,rs_upload,rs_retention;

CREATE FUNCTION aspect_project_allowed(w uuid,p uuid) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT NOT EXISTS(SELECT FROM source WHERE workspace_id=w AND project_id=p AND kind<>'synthetic_demo')
$$;
REVOKE ALL ON FUNCTION aspect_project_allowed(uuid,uuid) FROM PUBLIC;

CREATE FUNCTION aspect_scope(w uuid,p uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF w IS DISTINCT FROM nullif(current_setting('rs.workspace',true),'')::uuid THEN RETURN false; END IF;
 PERFORM FROM client_project cp JOIN workspace ws ON ws.id=cp.workspace_id
 JOIN workspace_membership m ON m.workspace_id=ws.id JOIN principal u ON u.id=m.principal_id
 JOIN access_session x ON x.principal_id=u.id
 WHERE (cp.workspace_id,cp.id)=(w,p) AND ws.state='active' AND m.state='active' AND m.role IN ('owner','analyst')
 AND u.id=access_actor() AND u.state='active' AND x.revoked_at IS NULL AND x.expires_at>statement_timestamp()
 AND x.token_hash=sha256(convert_to(current_setting('rs.session_proof',true),'UTF8'))
 AND aspect_project_allowed(w,p)
 FOR SHARE OF cp,ws,m,u,x;
 RETURN FOUND;
END $$;

CREATE FUNCTION save_aspect(w uuid,p uuid,a uuid,expected uuid,k uuid,d jsonb,origin_ref text,archive boolean)
RETURNS TABLE(code integer,aspect_id uuid,version_id uuid) LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE permitted integer:=job_writer(w); who uuid:=access_actor(); prior aspect_version; head aspect; saved uuid; num integer;
BEGIN
 IF permitted<>200 OR NOT aspect_scope(w,p) THEN RETURN QUERY SELECT 404,NULL::uuid,NULL::uuid; RETURN; END IF;
 IF k IS NULL OR archive IS NULL OR (a IS NULL)<>(expected IS NULL)
 OR (archive AND (d IS NOT NULL OR origin_ref IS NOT NULL OR a IS NULL))
 OR (NOT archive AND (aspect_payload_valid(d) IS NOT TRUE OR aspect_origin_valid(origin_ref) IS NOT TRUE))
 THEN RETURN QUERY SELECT 422,NULL::uuid,NULL::uuid; RETURN; END IF;
 SELECT * INTO prior FROM aspect_version v WHERE (v.workspace_id,v.project_id,v.request_key)=(w,p,k);
 IF FOUND THEN
   IF NOT archive AND (prior.created_by,prior.payload,prior.origin,prior.expected_version) IS NOT DISTINCT FROM
      (who,d,origin_ref,expected) AND (a IS NULL OR a=prior.aspect_id)
   THEN RETURN QUERY SELECT 200,prior.aspect_id,prior.version_id;
   ELSE RETURN QUERY SELECT 409,NULL::uuid,NULL::uuid; END IF;
   RETURN;
 END IF;
 SELECT * INTO head FROM aspect h WHERE (h.workspace_id,h.project_id,h.archive_key)=(w,p,k);
 IF FOUND THEN
   IF archive AND head.archived_by=who AND head.aspect_id=a AND EXISTS(SELECT FROM aspect_version v WHERE
     (v.workspace_id,v.project_id,v.aspect_id,v.version_no,v.version_id)=(w,p,a,head.current_no,expected))
   THEN RETURN QUERY SELECT 200,a,expected; ELSE RETURN QUERY SELECT 409,NULL::uuid,NULL::uuid; END IF;
   RETURN;
 END IF;
 IF a IS NULL THEN
   num:=1;
   INSERT INTO aspect(workspace_id,project_id,created_by,current_no) VALUES(w,p,who,num) RETURNING aspect.aspect_id INTO a;
 ELSE
   SELECT * INTO head FROM aspect h WHERE (h.workspace_id,h.project_id,h.aspect_id)=(w,p,a) FOR UPDATE;
   IF NOT FOUND THEN RETURN QUERY SELECT 404,NULL::uuid,NULL::uuid; RETURN; END IF;
   IF head.archived_at IS NOT NULL OR NOT EXISTS(SELECT FROM aspect_version v WHERE
      (v.workspace_id,v.project_id,v.aspect_id,v.version_no,v.version_id)=(w,p,a,head.current_no,expected))
   THEN RETURN QUERY SELECT 409,NULL::uuid,NULL::uuid; RETURN; END IF;
   IF archive THEN
     UPDATE aspect h SET archive_key=k,archived_at=now(),archived_by=who WHERE (h.workspace_id,h.project_id,h.aspect_id)=(w,p,a);
     RETURN QUERY SELECT 200,a,expected; RETURN;
   END IF;
   num:=head.current_no+1;
   UPDATE aspect h SET current_no=num WHERE (h.workspace_id,h.project_id,h.aspect_id)=(w,p,a);
 END IF;
 INSERT INTO aspect_version(workspace_id,project_id,aspect_id,version_no,payload,origin,request_key,expected_version,created_by)
 VALUES(w,p,a,num,d,origin_ref,k,expected,who) RETURNING aspect_version.version_id INTO saved;
 RETURN QUERY SELECT 201,a,saved;
END $$;

CREATE FUNCTION aspect_page(w uuid,p uuid,a uuid,skip integer) RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT aspect_scope(w,p) OR skip IS NULL OR skip<0 THEN RETURN; END IF;
 IF a IS NOT NULL THEN
   RETURN QUERY SELECT jsonb_build_object('aspect_id',a,'version_id',v.version_id,'version_no',v.version_no,
    'name',v.payload->'name','archived',h.archived_at IS NOT NULL) FROM aspect_version v
    JOIN aspect h USING(workspace_id,project_id,aspect_id) WHERE (v.workspace_id,v.project_id,v.aspect_id)=(w,p,a)
    ORDER BY v.version_no DESC LIMIT 25 OFFSET skip;
 ELSE
   RETURN QUERY SELECT jsonb_build_object('aspect_id',h.aspect_id,'version_id',v.version_id,'version_no',v.version_no,
    'name',v.payload->'name','archived',h.archived_at IS NOT NULL) FROM (SELECT aspect_id,current_no,archived_at FROM aspect
      WHERE (workspace_id,project_id)=(w,p) ORDER BY aspect_id LIMIT 25 OFFSET skip) h JOIN aspect_version v
    ON (v.workspace_id,v.project_id,v.aspect_id,v.version_no)=(w,p,h.aspect_id,h.current_no)
    ORDER BY h.aspect_id;
 END IF;
END $$;
CREATE FUNCTION aspect_detail(w uuid,p uuid,a uuid,v_id uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('workspace_id',w,'project_id',p,'aspect_id',a,'version_id',v_id,'version_no',v.version_no,
 'payload',v.payload,'digest',v.digest,'origin',v.origin,'created_by',v.created_by,'created_at',v.created_at,
 'archived',h.archived_at IS NOT NULL,'current_version_id',head.version_id)
 FROM aspect h JOIN aspect_version v USING(workspace_id,project_id,aspect_id) JOIN aspect_version head ON
 (head.workspace_id,head.project_id,head.aspect_id,head.version_no)=(w,p,a,h.current_no)
 WHERE (v.workspace_id,v.project_id,v.aspect_id,v.version_id)=(w,p,a,v_id) AND aspect_scope(w,p)
$$;
REVOKE ALL ON FUNCTION aspect_nonblank(text),aspect_digest(jsonb),aspect_payload_valid(jsonb),aspect_origin_valid(text),aspect_scope(uuid,uuid),
 save_aspect(uuid,uuid,uuid,uuid,uuid,jsonb,text,boolean),aspect_page(uuid,uuid,uuid,integer),aspect_detail(uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION aspect_scope(uuid,uuid),save_aspect(uuid,uuid,uuid,uuid,uuid,jsonb,text,boolean),
 aspect_page(uuid,uuid,uuid,integer),aspect_detail(uuid,uuid,uuid,uuid) TO rs_runtime;

-- Old requests keep NULL generated keys and their original four-field parameters.
ALTER TABLE analysis_run
 ADD COLUMN bound_project uuid GENERATED ALWAYS AS ((candidate_params#>>'{aspect,project_id}')::uuid) STORED,
 ADD COLUMN bound_source uuid GENERATED ALWAYS AS ((candidate_params#>>'{aspect,source_id}')::uuid) STORED,
 ADD COLUMN bound_aspect uuid GENERATED ALWAYS AS ((candidate_params#>>'{aspect,aspect_id}')::uuid) STORED,
 ADD COLUMN bound_version uuid GENERATED ALWAYS AS ((candidate_params#>>'{aspect,version_id}')::uuid) STORED,
 ADD COLUMN bound_digest text GENERATED ALWAYS AS (candidate_params#>>'{aspect,digest}') STORED;
ALTER TABLE analysis_run
 ADD CHECK (NOT candidate_params ? 'aspect' OR num_nonnulls(bound_project,bound_source,bound_aspect,bound_version,bound_digest)=5),
 ADD FOREIGN KEY(workspace_id,bound_project,bound_source) REFERENCES source(workspace_id,project_id,id),
 ADD FOREIGN KEY(workspace_id,bound_source,snapshot_id) REFERENCES snapshot(workspace_id,source_id,id),
 ADD FOREIGN KEY(workspace_id,bound_project,bound_aspect,bound_version,bound_digest)
 REFERENCES aspect_version(workspace_id,project_id,aspect_id,version_id,digest);
CREATE FUNCTION enqueue_candidate(w uuid,src uuid,s uuid,k uuid,q text,m text,role_name text,top_k integer,a uuid,v uuid)
RETURNS TABLE(code integer,run_id uuid) LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE prior analysis_run; permitted integer := job_writer(w); params jsonb; binding jsonb; archived boolean;
BEGIN
 IF permitted<>200 THEN RETURN QUERY SELECT permitted,NULL::uuid; RETURN; END IF;
 IF NOT candidate_scope(w,src,s) THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
 IF num_nulls(k,q,m,role_name,top_k)>0 OR octet_length(q) NOT BETWEEN 1 AND 2048 OR q !~ '[^[:space:]]'
 OR m NOT IN ('lexical','dense','hybrid') OR role_name NOT IN ('query_to_passage','text_to_text')
 OR top_k NOT BETWEEN 1 AND 10 THEN RETURN QUERY SELECT 422,NULL::uuid; RETURN; END IF;
 params:=jsonb_build_object('query',q,'mode',m,'role',role_name,'k',top_k);
 IF (a IS NULL)<>(v IS NULL) THEN RETURN QUERY SELECT 422,NULL::uuid; RETURN; END IF;
 IF a IS NOT NULL THEN
   SELECT jsonb_build_object('project_id',z.project_id,'source_id',src,'aspect_id',a,'version_id',v,'digest',d.digest),
     h.archived_at IS NOT NULL INTO binding,archived FROM source z JOIN aspect h
     ON (h.workspace_id,h.project_id,h.aspect_id)=(w,z.project_id,a) JOIN aspect_version d ON (d.workspace_id,d.project_id,d.aspect_id)=(w,h.project_id,a)
     WHERE (z.workspace_id,z.id,d.version_id)=(w,src,v) AND aspect_scope(w,z.project_id) FOR SHARE OF h;
   IF NOT FOUND THEN RETURN QUERY SELECT 404,NULL::uuid; RETURN; END IF;
   params:=params||jsonb_build_object('aspect',binding);
 END IF;
 SELECT * INTO prior FROM analysis_run WHERE workspace_id=w AND request_key=k;
 IF FOUND THEN
   IF (prior.snapshot_id,prior.candidate_params,prior.requested_by) IS DISTINCT FROM (s,params,access_actor())
   THEN RETURN QUERY SELECT 409,NULL::uuid; ELSE RETURN QUERY SELECT 200,prior.id; END IF;
   RETURN;
 END IF;
 IF archived THEN RETURN QUERY SELECT 409,NULL::uuid; RETURN; END IF;
 IF (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND status IN ('queued','running'))>=4 OR
    (SELECT count(*) FROM analysis_run WHERE workspace_id=w AND request_key IS NOT NULL AND created_at>clock_timestamp()-interval '1 hour')>=20
 THEN RETURN QUERY SELECT 429,NULL::uuid; RETURN; END IF;
 INSERT INTO analysis_run(workspace_id,snapshot_id,job_kind,request_key,requested_by,engine_version,definition_version,
    model_version,stage,ready_at,candidate_params) VALUES(w,s,'snapshot_candidate_search_v1',k,access_actor(),'1',
    'candidate-search-1',CASE WHEN m='lexical' THEN 'unused' ELSE 'minilm-pr04' END,'queued',clock_timestamp(),params)
 RETURNING id INTO run_id;
 code:=202; RETURN NEXT;
END $$;
REVOKE ALL ON FUNCTION enqueue_candidate(uuid,uuid,uuid,uuid,text,text,text,integer,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION enqueue_candidate(uuid,uuid,uuid,uuid,text,text,text,integer,uuid,uuid) TO rs_runtime;


CREATE OR REPLACE FUNCTION enqueue_candidate(w uuid,src uuid,s uuid,k uuid,q text,m text,role_name text,top_k integer)
RETURNS TABLE(code integer,run_id uuid) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT * FROM enqueue_candidate(w,src,s,k,q,m,role_name,top_k,NULL,NULL)
$$;

ALTER FUNCTION job_authorized(analysis_run) RENAME TO pre_aspect_job_authorized;
CREATE FUNCTION job_authorized(j analysis_run) RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT pre_aspect_job_authorized(j) AND (j.bound_version IS NULL OR aspect_project_allowed(j.workspace_id,j.bound_project))
$$;
REVOKE ALL ON FUNCTION job_authorized(analysis_run) FROM PUBLIC;

ALTER FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) RENAME TO pre_aspect_step;
REVOKE ALL ON FUNCTION pre_aspect_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM rs_worker;
CREATE FUNCTION worker_step(w uuid,r uuid,t uuid,next_stage text,n integer,den integer,
 problem text DEFAULT NULL,digest sha256_hex DEFAULT NULL,bytes integer DEFAULT NULL,manifest sha256_hex DEFAULT NULL)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE p uuid; j analysis_run;
BEGIN
 IF NOT coalesce(retention_online(),false) THEN RETURN 'stale'; END IF;
 SELECT bound_project INTO p FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t);
 IF p IS NOT NULL THEN
   -- Same order as enqueue. Source insertion takes a key-share lock on this project.
   PERFORM FROM workspace WHERE id=w FOR SHARE;
   PERFORM FROM client_project WHERE (workspace_id,id)=(w,p) FOR UPDATE;
   IF NOT aspect_project_allowed(w,p) THEN
     SELECT * INTO j FROM analysis_run WHERE (workspace_id,id,lease_token)=(w,r,t)
       AND status='running' AND lease_until>clock_timestamp() FOR UPDATE;
     IF NOT FOUND THEN RETURN 'stale'; END IF;
     RETURN stop_attempt(j,CASE WHEN j.cancel_requested THEN 'CANCELLED' ELSE 'ACCESS_REVOKED' END);
   END IF;
 END IF;
 RETURN pre_aspect_step(w,r,t,next_stage,n,den,problem,digest,bytes,manifest);
END $$;
REVOKE ALL ON FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION worker_step(uuid,uuid,uuid,text,integer,integer,text,sha256_hex,integer,sha256_hex) TO rs_worker;

CREATE FUNCTION verify_aspect_publication() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE params jsonb;
BEGIN
 SELECT candidate_params INTO params FROM analysis_run WHERE (workspace_id,id)=(NEW.workspace_id,NEW.run_id);
 IF params IS NOT NULL AND (current_setting('rs.candidate_projection',true)::jsonb->'aspect') IS DISTINCT FROM params->'aspect'
 THEN RAISE EXCEPTION 'invalid_aspect_binding'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER verify_aspect_publication BEFORE INSERT ON artifact FOR EACH ROW EXECUTE FUNCTION verify_aspect_publication();

CREATE FUNCTION candidate_definition(w uuid,src uuid,s uuid,r uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('query',CASE WHEN j.bound_version IS NOT NULL THEN j.candidate_params->'query' END,
 'query_sha256',encode(sha256(convert_to(j.candidate_params->>'query','UTF8')),'hex'),'aspect',j.candidate_params->'aspect',
 'definition',aspect_detail(w,j.bound_project,j.bound_aspect,j.bound_version)-ARRAY['current_version_id','archived'])
 FROM analysis_run j JOIN artifact art ON (art.workspace_id,art.run_id,art.id)=(j.workspace_id,j.id,j.output_artifact_id)
 WHERE (j.workspace_id,j.snapshot_id,j.id)=(w,s,r) AND j.job_kind='snapshot_candidate_search_v1' AND j.status='succeeded'
 AND art.storage_state='available' AND art.size_bytes BETWEEN 1 AND 65536 AND candidate_scope(w,src,s)
 AND (j.bound_version IS NULL OR aspect_project_allowed(w,j.bound_project))
$$;
REVOKE ALL ON FUNCTION verify_aspect_publication(),candidate_definition(uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION candidate_definition(uuid,uuid,uuid,uuid) TO rs_runtime;

ALTER FUNCTION retention_plan(uuid,text,uuid) RENAME TO pre_aspect_retention_plan;
CREATE FUNCTION retention_plan(w uuid,k text,t uuid) RETURNS jsonb LANGUAGE sql STABLE
SET search_path=pg_catalog,public,pg_temp AS $$
 WITH plan AS (SELECT pre_aspect_retention_plan(w,k,t) AS data)
 SELECT jsonb_set(data,'{tables}',(data->'tables')||jsonb_build_object(
 'aspect',(SELECT count(*) FROM aspect WHERE workspace_id=w AND (k='workspace' OR k='project' AND project_id=t)),
 'archived_aspects',(SELECT count(*) FROM aspect WHERE workspace_id=w AND archived_at IS NOT NULL AND (k='workspace' OR k='project' AND project_id=t)),
 'aspect_version',(SELECT count(*) FROM aspect_version WHERE workspace_id=w AND (k='workspace' OR k='project' AND project_id=t))))
 FROM plan
$$;
REVOKE ALL ON FUNCTION retention_plan(uuid,text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION aspect_digest(jsonb) TO rs_retention;
CREATE FUNCTION aspect_exportable(w uuid,p uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT export_project(w,p) AND aspect_project_allowed(w,p)
$$;
REVOKE ALL ON FUNCTION aspect_exportable(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION aspect_exportable(uuid,uuid) TO rs_retention;
CREATE POLICY bound_export ON analysis_run AS RESTRICTIVE FOR SELECT TO rs_retention
 USING(bound_project IS NULL OR aspect_exportable(workspace_id,bound_project));
CREATE POLICY owner_export ON aspect FOR SELECT TO rs_retention USING(aspect_exportable(workspace_id,project_id)
 AND EXISTS(SELECT FROM retention_request WHERE id=nullif(current_setting('rs.export_request',true),'')::uuid AND kind<>'source'));
CREATE POLICY owner_export ON aspect_version FOR SELECT TO rs_retention USING(EXISTS(
 SELECT FROM aspect h WHERE (h.workspace_id,h.project_id,h.aspect_id)=(aspect_version.workspace_id,aspect_version.project_id,aspect_version.aspect_id)));
GRANT SELECT(workspace_id,project_id,aspect_id,current_no,created_at,created_by,archived_at,archived_by) ON aspect TO rs_retention;
GRANT SELECT(workspace_id,project_id,aspect_id,version_no,version_id,payload,digest,origin,created_by,created_at) ON aspect_version TO rs_retention;
