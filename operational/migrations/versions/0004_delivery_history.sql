-- 0004 holds exclusive locks; all DDL, the two metadata fills and the stamp commit together.
ALTER TABLE review_identity ADD COLUMN external_place_id nonblank;
ALTER TABLE review_identity DISABLE TRIGGER immutable_rows;
UPDATE review_identity i SET external_place_id=v.place
FROM (SELECT workspace_id,source_id,identity_id,min(external_place_id) place
      FROM review_version GROUP BY workspace_id,source_id,identity_id) v
WHERE (i.workspace_id,i.source_id,i.id)=(v.workspace_id,v.source_id,v.identity_id);
ALTER TABLE review_identity ENABLE TRIGGER immutable_rows;
ALTER TABLE review_identity
    ALTER COLUMN external_place_id SET NOT NULL,
    DROP CONSTRAINT review_identity_workspace_id_source_id_external_review_id_key,
    ADD FOREIGN KEY (workspace_id,source_id,external_place_id) REFERENCES source_location,
    ADD UNIQUE (workspace_id,source_id,external_place_id,external_review_id),
    ADD UNIQUE (workspace_id,source_id,id,external_place_id),
    ADD COLUMN observation_batch_id uuid,
    ADD COLUMN observation_position integer CHECK (observation_position > 0),
    ADD CHECK ((observation_batch_id IS NULL)=(observation_position IS NULL)
        AND (observation_batch_id IS NULL OR external_review_id IS NULL)),
    ADD UNIQUE (workspace_id,source_id,observation_batch_id,observation_position);

ALTER TABLE review_version
    ALTER COLUMN external_place_id SET NOT NULL,
    ADD FOREIGN KEY (workspace_id,source_id,identity_id,external_place_id)
        REFERENCES review_identity(workspace_id,source_id,id,external_place_id),
    ADD UNIQUE (workspace_id,source_id,identity_id,id),
    ADD COLUMN event_date date,
    ADD COLUMN event_at timestamptz,
    ADD CHECK (num_nonnulls(event_date,event_at) <= 1);

-- Only the complete old two-bucket equation establishes a zero residual.
ALTER TABLE import_batch ADD COLUMN confirmed_duplicate_count bigint CHECK (confirmed_duplicate_count >= 0),
    ADD COLUMN delivery jsonb,
    ADD COLUMN registered_by uuid REFERENCES principal;
ALTER TABLE import_batch DISABLE TRIGGER immutable_rows;
UPDATE import_batch SET confirmed_duplicate_count=0
WHERE num_nonnulls(input_count,accepted_count,rejected_count)=3;
ALTER TABLE import_batch ENABLE TRIGGER immutable_rows;
DO $$
DECLARE old_check name;
BEGIN
    SELECT conname INTO STRICT old_check FROM pg_constraint WHERE conrelid='import_batch'::regclass
      AND pg_get_expr(conbin,conrelid)='(input_count = (accepted_count + rejected_count))';
    EXECUTE format('ALTER TABLE import_batch DROP CONSTRAINT %I',old_check);
END $$;
ALTER TABLE import_batch
    ADD CHECK (num_nonnulls(input_count,accepted_count,rejected_count)<3 OR confirmed_duplicate_count IS NOT NULL),
    ADD CHECK (input_count = accepted_count + rejected_count + confirmed_duplicate_count),
    ADD CHECK (delivery IS NULL OR (registered_by IS NOT NULL AND jsonb_typeof(delivery)='object' AND
        delivery ?& ARRAY['fingerprint','mapping_sha256','passport_sha256','coverage_sha256','format','coverage_passport']
        AND num_nonnulls(input_count,accepted_count,rejected_count,confirmed_duplicate_count)=4));

-- Old callers can still insert a complete, exhaustive two-outcome receipt.
CREATE FUNCTION legacy_receipt_counts() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.confirmed_duplicate_count := 0;
    RETURN NEW;
END $$;
CREATE TRIGGER legacy_counts BEFORE INSERT ON import_batch FOR EACH ROW
    WHEN (NEW.delivery IS NULL AND NEW.confirmed_duplicate_count IS NULL AND
          NEW.input_count=NEW.accepted_count+NEW.rejected_count)
    EXECUTE FUNCTION legacy_receipt_counts();

-- Object keys are workspace-scoped and must resolve to the same archive for every login and source.
ALTER TABLE workspace ADD COLUMN delivery_object_root nonblank;

-- This grant confirms permission to retain the source bytes.
CREATE TABLE delivery_authorization (
    workspace_id uuid NOT NULL, source_id uuid NOT NULL, operator_login name NOT NULL,
    principal_id uuid NOT NULL,
    PRIMARY KEY (workspace_id,source_id,operator_login),
    FOREIGN KEY (workspace_id,source_id) REFERENCES source,
    FOREIGN KEY (workspace_id,principal_id) REFERENCES workspace_membership,
    CHECK (operator_login <> 'rs_runtime')
);
ALTER TABLE delivery_authorization ENABLE ROW LEVEL SECURITY;

CREATE TABLE import_row (
    workspace_id uuid NOT NULL, source_id uuid NOT NULL, batch_id uuid NOT NULL,
    position integer NOT NULL CHECK (position > 0),
    status text NOT NULL CHECK (status IN ('accepted','confirmed_duplicate','quarantined')),
    reasons text[] NOT NULL, warnings text[] NOT NULL,
    related_position integer, identity_id uuid, version_id uuid,
    provenance jsonb NOT NULL, field_presence jsonb NOT NULL,
    PRIMARY KEY (workspace_id,source_id,batch_id,position),
    FOREIGN KEY (workspace_id,source_id,batch_id) REFERENCES import_batch,
    FOREIGN KEY (workspace_id,source_id,identity_id,version_id) REFERENCES review_version(workspace_id,source_id,identity_id,id),
    FOREIGN KEY (workspace_id,source_id,batch_id,related_position) REFERENCES import_row DEFERRABLE INITIALLY DEFERRED,
    CHECK ((status='quarantined')=(version_id IS NULL) AND (identity_id IS NULL)=(version_id IS NULL)),
    CHECK ((status='accepted')=(cardinality(reasons)=0)),
    CHECK (status<>'confirmed_duplicate' OR (related_position IS NOT NULL AND related_position<position))
);
ALTER TABLE review_identity ADD FOREIGN KEY (workspace_id,source_id,observation_batch_id,observation_position)
    REFERENCES import_row DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE import_row ENABLE ROW LEVEL SECURITY;
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON import_row
    FOR EACH ROW EXECUTE FUNCTION reject_history_change();
CREATE TRIGGER immutable_table BEFORE TRUNCATE ON import_row
    FOR EACH STATEMENT EXECUTE FUNCTION reject_history_change();

CREATE FUNCTION guard_import_position() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT FROM import_batch WHERE (workspace_id,source_id,id)=
        (NEW.workspace_id,NEW.source_id,NEW.batch_id) AND delivery IS NOT NULL AND NEW.position<=input_count)
      OR EXISTS (SELECT FROM review_identity WHERE (workspace_id,source_id,id)=
        (NEW.workspace_id,NEW.source_id,NEW.identity_id) AND external_review_id IS NULL AND
        (observation_batch_id,observation_position) IS DISTINCT FROM (NEW.batch_id,NEW.position))
      OR (NEW.status='confirmed_duplicate' AND NOT EXISTS (SELECT FROM import_row WHERE
        (workspace_id,source_id,batch_id,position,version_id,status)=
        (NEW.workspace_id,NEW.source_id,NEW.batch_id,NEW.related_position,NEW.version_id,'accepted'))) THEN
        RAISE EXCEPTION 'invalid receipt position' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER valid_position BEFORE INSERT ON import_row FOR EACH ROW EXECUTE FUNCTION guard_import_position();

CREATE UNIQUE INDEX canonical_batch_snapshot ON snapshot(workspace_id,source_id,batch_id)
    WHERE filters->>'format'='canonical-json-v1';
CREATE FUNCTION check_delivery_seal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a integer; q integer; d integer; members uuid[];
BEGIN
    SELECT count(*) FILTER (WHERE status='accepted'), count(*) FILTER (WHERE status='quarantined'),
           count(*) FILTER (WHERE status='confirmed_duplicate'),
           coalesce(array_agg(version_id ORDER BY version_id) FILTER (WHERE status='accepted'),'{}')
    INTO a,q,d,members FROM import_row
    WHERE (workspace_id,source_id,batch_id)=(NEW.workspace_id,NEW.source_id,NEW.id);
    IF (a,q,d)<>(NEW.accepted_count,NEW.rejected_count,NEW.confirmed_duplicate_count)
       OR NOT EXISTS (SELECT FROM snapshot WHERE (workspace_id,source_id,batch_id)=
            (NEW.workspace_id,NEW.source_id,NEW.id) AND filters->>'format'='canonical-json-v1'
            AND version_ids=members) THEN
        RAISE EXCEPTION 'incomplete delivery seal' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER complete_delivery AFTER INSERT ON import_batch DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.delivery IS NOT NULL) EXECUTE FUNCTION check_delivery_seal();
REVOKE EXECUTE ON FUNCTION guard_import_position(),check_delivery_seal(),legacy_receipt_counts() FROM PUBLIC;
