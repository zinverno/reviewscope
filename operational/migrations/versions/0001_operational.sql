CREATE DOMAIN sha256_hex AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');
CREATE DOMAIN coverage_kind AS text CHECK (VALUE IN ('unknown', 'partial', 'full'));
CREATE DOMAIN nonblank AS text CHECK (VALUE ~ '[^[:space:]]');

CREATE TABLE workspace (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name nonblank NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','archived'))
);
CREATE TABLE workspace_membership (
    workspace_id uuid NOT NULL REFERENCES workspace,
    principal_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('owner','analyst','viewer')),
    state text NOT NULL CHECK (state IN ('active','inactive')),
    PRIMARY KEY (workspace_id, principal_id)
);
CREATE TABLE client_project (
    workspace_id uuid NOT NULL REFERENCES workspace,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    name nonblank NOT NULL,
    PRIMARY KEY (workspace_id,id)
);
CREATE TABLE location (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    name nonblank,
    PRIMARY KEY (workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id) REFERENCES client_project
);
CREATE TABLE source (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    source_key nonblank NOT NULL,
    kind text NOT NULL CHECK (kind IN ('synthetic_demo','file_export')),
    origin_label nonblank NOT NULL,
    external_source_id nonblank,
    coverage_policy coverage_kind,
    PRIMARY KEY (workspace_id,id),
    UNIQUE (workspace_id,project_id,id),
    UNIQUE (workspace_id,project_id,source_key),
    FOREIGN KEY (workspace_id,project_id) REFERENCES client_project
);
CREATE TABLE source_location (
    workspace_id uuid NOT NULL,
    project_id uuid NOT NULL,
    source_id uuid NOT NULL,
    external_place_id nonblank NOT NULL,
    location_id uuid NOT NULL,
    PRIMARY KEY (workspace_id,source_id,external_place_id),
    FOREIGN KEY (workspace_id,project_id,source_id) REFERENCES source(workspace_id,project_id,id),
    FOREIGN KEY (workspace_id,project_id,location_id) REFERENCES location
);
CREATE TABLE import_batch (
    workspace_id uuid NOT NULL,
    source_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    registered_at timestamptz NOT NULL DEFAULT now(),
    received_at timestamptz,
    covered_from timestamptz,
    covered_until timestamptz,
    file_sha256 sha256_hex NOT NULL,
    procedure_version nonblank NOT NULL,
    coverage coverage_kind,
    status text NOT NULL CHECK (status IN ('accepted','rejected')),
    input_count bigint CHECK (input_count >= 0),
    accepted_count bigint CHECK (accepted_count >= 0),
    rejected_count bigint CHECK (rejected_count >= 0),
    PRIMARY KEY (workspace_id,source_id,id),
    FOREIGN KEY (workspace_id,source_id) REFERENCES source,
    CHECK (covered_until > covered_from),
    CHECK (accepted_count <= input_count AND rejected_count <= input_count),
    CHECK (input_count = accepted_count + rejected_count)
);
CREATE INDEX import_fingerprint_idx ON import_batch(workspace_id,source_id,file_sha256,procedure_version);
CREATE TABLE review_identity (
    workspace_id uuid NOT NULL,
    source_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    external_review_id nonblank,
    PRIMARY KEY (workspace_id,source_id,id),
    UNIQUE (workspace_id,source_id,external_review_id),
    FOREIGN KEY (workspace_id,source_id) REFERENCES source
);
CREATE TABLE review_version (
    workspace_id uuid NOT NULL,
    source_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    identity_id uuid NOT NULL,
    batch_id uuid NOT NULL,
    version_no integer NOT NULL CHECK (version_no >= 1),
    supersedes_no integer GENERATED ALWAYS AS (nullif(version_no - 1, 0)) STORED,
    normalization_version nonblank NOT NULL,
    source_reviewer_id nonblank,
    external_place_id nonblank,
    text_content text,
    rating smallint CHECK (rating BETWEEN 1 AND 5),
    published_at timestamptz,
    published_date date,
    metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    content_sha256 sha256_hex NOT NULL,
    PRIMARY KEY (workspace_id,source_id,id),
    UNIQUE (workspace_id,source_id,identity_id,version_no),
    FOREIGN KEY (workspace_id,source_id,identity_id,supersedes_no)
        REFERENCES review_version(workspace_id,source_id,identity_id,version_no),
    FOREIGN KEY (workspace_id,source_id,identity_id) REFERENCES review_identity,
    FOREIGN KEY (workspace_id,source_id,batch_id) REFERENCES import_batch,
    FOREIGN KEY (workspace_id,source_id,external_place_id) REFERENCES source_location,
    CHECK (num_nonnulls(published_at,published_date) <= 1)
);
CREATE INDEX review_version_batch_idx ON review_version(workspace_id,source_id,batch_id);
CREATE FUNCTION review_version_hash() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT FROM import_batch WHERE (workspace_id,source_id,id) =
              (NEW.workspace_id,NEW.source_id,NEW.batch_id) AND status='rejected') THEN
        RAISE EXCEPTION 'rejected import cannot publish versions' USING ERRCODE = '23514';
    END IF;
    NEW.content_sha256 := encode(sha256(convert_to(jsonb_build_array(
        NEW.normalization_version, NEW.source_reviewer_id, NEW.external_place_id, NEW.text_content,
        NEW.rating, extract(epoch FROM NEW.published_at), to_char(NEW.published_date,'YYYY-MM-DD'),
        NEW.metadata)::text,'UTF8')),'hex');
    RETURN NEW;
END $$;
CREATE TRIGGER version_hash BEFORE INSERT ON review_version
    FOR EACH ROW EXECUTE FUNCTION review_version_hash();

-- Canonical manifest header plus one ordered version UUID per line. The import receipt
-- fixes the covered interval and coverage. Member IDs resolve only within this source.
CREATE FUNCTION snapshot_digest(w uuid, s uuid, b uuid, object_hash sha256_hex, filters jsonb, ids uuid[])
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT encode(sha256(convert_to(concat_ws(chr(10), 'rs-snapshot-v1', w, s, b,
        object_hash, filters::text, array_to_string(ids,chr(10))), 'UTF8')), 'hex')
$$;
CREATE FUNCTION object_key(w uuid, digest sha256_hex) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT w::text || '/sha256/' || digest $$;

CREATE TABLE snapshot (
    workspace_id uuid NOT NULL,
    source_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    batch_id uuid NOT NULL,
    filters jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(filters) = 'object'),
    duckdb_sha256 sha256_hex NOT NULL,
    duckdb_key text GENERATED ALWAYS AS (object_key(workspace_id,duckdb_sha256)) STORED,
    version_ids uuid[] NOT NULL,
    manifest_sha256 sha256_hex NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id,id),
    UNIQUE (workspace_id,source_id,id),
    UNIQUE (workspace_id,source_id,manifest_sha256),
    FOREIGN KEY (workspace_id,source_id,batch_id) REFERENCES import_batch,
    CHECK (manifest_sha256 = snapshot_digest(workspace_id,source_id,batch_id,duckdb_sha256,filters,version_ids))
);
CREATE TABLE snapshot_member (
    workspace_id uuid NOT NULL,
    source_id uuid NOT NULL,
    snapshot_id uuid NOT NULL,
    version_id uuid NOT NULL,
    PRIMARY KEY (workspace_id,snapshot_id,version_id),
    FOREIGN KEY (workspace_id,source_id,snapshot_id) REFERENCES snapshot(workspace_id,source_id,id),
    FOREIGN KEY (workspace_id,source_id,version_id) REFERENCES review_version
);
CREATE INDEX snapshot_member_version_idx ON snapshot_member(workspace_id,source_id,version_id);
CREATE FUNCTION publish_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    identities integer;
BEGIN
    IF EXISTS (SELECT FROM import_batch WHERE (workspace_id,source_id,id) =
              (NEW.workspace_id,NEW.source_id,NEW.batch_id) AND status='rejected') THEN
        RAISE EXCEPTION 'rejected import cannot publish snapshots' USING ERRCODE = '23514';
    END IF;
    IF NEW.version_ids <> ARRAY(SELECT DISTINCT v FROM unnest(NEW.version_ids) v ORDER BY v)
       OR array_position(NEW.version_ids,NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'manifest requires sorted distinct version IDs' USING ERRCODE = '23514';
    END IF;
    SELECT count(DISTINCT identity_id) INTO identities FROM review_version
      WHERE workspace_id=NEW.workspace_id AND source_id=NEW.source_id AND id=ANY(NEW.version_ids);
    IF identities <> cardinality(NEW.version_ids) THEN
        RAISE EXCEPTION 'unknown version or repeated identity in manifest' USING ERRCODE = '23514';
    END IF;
    INSERT INTO snapshot_member SELECT NEW.workspace_id, NEW.source_id, NEW.id, unnest(NEW.version_ids);
    RETURN NULL;
END $$;
CREATE TRIGGER snapshot_publish AFTER INSERT ON snapshot FOR EACH ROW EXECUTE FUNCTION publish_snapshot();

CREATE FUNCTION check_member() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT FROM snapshot WHERE workspace_id=NEW.workspace_id AND source_id=NEW.source_id
                   AND id=NEW.snapshot_id AND NEW.version_id=ANY(version_ids)) THEN
        RAISE EXCEPTION 'member outside sealed manifest' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sealed_member BEFORE INSERT ON snapshot_member FOR EACH ROW EXECUTE FUNCTION check_member();

CREATE TABLE analysis_run (
    workspace_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    snapshot_id uuid NOT NULL,
    engine_version nonblank NOT NULL,
    model_version nonblank NOT NULL,
    definition_version nonblank NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed')),
    output_artifact_id uuid,
    PRIMARY KEY (workspace_id,id),
    FOREIGN KEY (workspace_id,snapshot_id) REFERENCES snapshot,
    CHECK ((status IN ('succeeded','failed')) = (finished_at IS NOT NULL)),
    CHECK ((status='succeeded') = (output_artifact_id IS NOT NULL)),
    CHECK (finished_at >= created_at)
);
CREATE INDEX analysis_run_snapshot_idx ON analysis_run(workspace_id,snapshot_id);
CREATE TABLE artifact (
    workspace_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('report','json','duckdb','export')),
    checksum sha256_hex NOT NULL,
    object_key text GENERATED ALWAYS AS (object_key(workspace_id,checksum)) STORED,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    storage_state text NOT NULL DEFAULT 'available' CHECK (storage_state IN ('available','pending_deletion','deleted')),
    PRIMARY KEY (workspace_id,id),
    UNIQUE (workspace_id,run_id,id),
    FOREIGN KEY (workspace_id,run_id) REFERENCES analysis_run
);
ALTER TABLE analysis_run ADD FOREIGN KEY (workspace_id,id,output_artifact_id)
    REFERENCES artifact(workspace_id,run_id,id);

CREATE FUNCTION guard_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status='queued' THEN RETURN NEW; END IF;
    ELSIF (to_jsonb(NEW) - ARRAY['status','finished_at','output_artifact_id'])
        = (to_jsonb(OLD) - ARRAY['status','finished_at','output_artifact_id'])
        AND ((OLD.status='queued' AND NEW.status IN ('running','failed'))
          OR (OLD.status='running' AND NEW.status IN ('succeeded','failed'))) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid analysis run transition' USING ERRCODE = '23514';
END $$;
CREATE TRIGGER run_transition BEFORE INSERT OR UPDATE ON analysis_run FOR EACH ROW EXECUTE FUNCTION guard_run();
CREATE FUNCTION guard_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        PERFORM FROM analysis_run WHERE workspace_id=NEW.workspace_id AND id=NEW.run_id AND status='running'
            FOR UPDATE;
        IF FOUND AND NEW.storage_state='available' THEN RETURN NEW; END IF;
    ELSIF (NEW.workspace_id,NEW.id,NEW.run_id,NEW.kind,NEW.checksum,NEW.size_bytes,NEW.created_at)
        IS NOT DISTINCT FROM (OLD.workspace_id,OLD.id,OLD.run_id,OLD.kind,OLD.checksum,OLD.size_bytes,OLD.created_at)
        AND ((OLD.storage_state='available' AND NEW.storage_state='pending_deletion')
          OR (OLD.storage_state='pending_deletion' AND NEW.storage_state='deleted')) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid artifact transition' USING ERRCODE = '23514';
END $$;
CREATE TRIGGER artifact_transition BEFORE INSERT OR UPDATE ON artifact FOR EACH ROW EXECUTE FUNCTION guard_artifact();

CREATE FUNCTION reject_history_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable operational history' USING ERRCODE = '23514';
END $$;
DO $$
DECLARE tab text;
BEGIN
    FOREACH tab IN ARRAY ARRAY['source','source_location','import_batch','review_identity','review_version',
                                'snapshot','snapshot_member','analysis_run','artifact'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE %s ON %I FOR EACH ROW EXECUTE FUNCTION reject_history_change()',
            CASE WHEN tab IN ('analysis_run','artifact') THEN 'DELETE' ELSE 'UPDATE OR DELETE' END, tab);
        EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION reject_history_change()',tab);
    END LOOP;
END $$;
