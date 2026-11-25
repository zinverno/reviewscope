"""Register the published, normalized synthetic DuckDB under an existing source."""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

import duckdb
import psycopg
from psycopg.types.json import Jsonb

from reviewscope.access.ledger import gate
from reviewscope.public_demo import DEFAULT_DEMO_DB, repo_root
from reviewscope.storage import DuckDBStore

# Only this reviewed artifact is admissible, even if a different manifest is supplied.
DEMO_SHA = "85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2"
PROCEDURE = "legacy-synthetic-duckdb-v1"


def verify_object(path: Path) -> None:
    with path.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != DEMO_SHA:
            raise ValueError("synthetic object checksum mismatch")


def import_demo(workspace: UUID, source: UUID, *, apply=False, object_root: Path | None = None):
    key = f"{workspace}/sha256/{DEMO_SHA}"
    if apply and object_root is None:
        raise ValueError("apply requires the staged immutable object")
    if object_root is not None:
        verify_object(object_root / key)
    demo = repo_root() / DEFAULT_DEMO_DB
    verify_object(demo)
    with DuckDBStore(demo, read_only=True) as store:
        reviews = store.fetch_reviews()
    namespace = uuid5(workspace, str(source))
    batch = uuid5(namespace, f"{PROCEDURE}:{DEMO_SHA}")
    snapshot = uuid5(batch, "snapshot")
    versions = []
    identities = []
    locations = {}
    for review in reviews:
        identity = uuid5(namespace, "review:" + review.review_id)
        version = uuid5(batch, str(identity))
        identities.append((workspace, source, identity, review.review_id, review.place_id))
        stamp = datetime.fromisoformat(review.published_at).replace(tzinfo=UTC) if review.published_at else None
        metadata = review.model_dump(exclude={
            "review_id", "reviewer_id", "place_id", "rating", "text", "published_at",
        })
        versions.append((workspace, source, version, identity, batch, PROCEDURE, review.reviewer_id,
                         review.place_id, review.text, review.rating, stamp, Jsonb(metadata)))
        locations[review.place_id] = (uuid5(namespace, "place:" + review.place_id), review.place_name)
    ids = sorted(row[2] for row in versions)
    manifest = "\n".join(["rs-snapshot-v1", str(workspace), str(source), str(batch), DEMO_SHA,
                          "{}", "\n".join(map(str, ids))])
    digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    summary = {
        "mode": "apply" if apply else "dry-run", "outcome": "planned",
        "batch_id": str(batch), "snapshot_id": str(snapshot), "reviews": len(ids),
        "locations": len(locations), "file_sha256": DEMO_SHA, "manifest_sha256": digest,
        "coverage": None, "object_key": key,
    }
    if not apply:
        return summary
    with psycopg.connect("") as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        if cur.fetchone()[0] in {"0006_retention", "0007_browser_delivery", "0008_review_evidence", "0009_overview", "0010_signals", "0011_reviewer_context", "0012_candidate_search", "0013_reviewer_context_plan", "0014_aspects", "0015_rerank", "0016_aspect_decisions", "0017_human_review"}:
            gate(cur)
        cur.execute("""SELECT project_id FROM source WHERE workspace_id=%s AND id=%s
                       AND kind='synthetic_demo' FOR UPDATE""", (workspace, source))
        owner = cur.fetchone()
        if owner is None:
            raise ValueError("expected an existing workspace-scoped synthetic source")
        project, = owner
        cur.execute("""SELECT manifest_sha256 FROM snapshot WHERE workspace_id=%s
                       AND source_id=%s AND id=%s AND batch_id=%s""", (workspace, source, snapshot, batch))
        existing = cur.fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("existing manifest differs")
            return summary | {"outcome": "already_applied"}

        cur.execute("""SELECT EXISTS(SELECT FROM import_batch WHERE workspace_id=%s AND source_id=%s)
                           OR EXISTS(SELECT FROM review_identity WHERE workspace_id=%s AND source_id=%s)
                           OR EXISTS(SELECT FROM source_location WHERE workspace_id=%s AND source_id=%s)""",
                    (workspace, source) * 3)
        if cur.fetchone()[0]:
            raise ValueError("initial backfill requires an unused synthetic source")
        cur.execute("SELECT version_num FROM alembic_version")
        revision, = cur.fetchone()
        historical = revision in {"0001_operational", "0002_principals", "0003_workspace_access"}
        if not historical and revision not in {"0004_delivery_history", "0005_background_jobs", "0006_retention", "0007_browser_delivery", "0008_review_evidence", "0009_overview", "0010_signals", "0011_reviewer_context", "0012_candidate_search", "0013_reviewer_context_plan", "0014_aspects", "0015_rerank", "0016_aspect_decisions", "0017_human_review"}:
            raise ValueError("unsupported operational revision")
        cur.execute("""INSERT INTO import_batch
                       (workspace_id,source_id,id,file_sha256,procedure_version,status,
                        input_count,accepted_count,rejected_count)
                       VALUES (%s,%s,%s,%s,%s,'accepted',%s,%s,0)""",
                    (workspace, source, batch, DEMO_SHA, PROCEDURE, len(ids), len(ids)))
        cur.executemany("INSERT INTO location(workspace_id,project_id,id,name) VALUES (%s,%s,%s,%s)",
                        [(workspace, project, location, name) for location, name in locations.values()])
        cur.executemany("""INSERT INTO source_location(workspace_id,project_id,source_id,external_place_id,location_id)
                           VALUES (%s,%s,%s,%s,%s)""",
                        [(workspace, project, source, place, location) for place, (location, _) in locations.items()])
        if historical:
            cur.executemany("""INSERT INTO review_identity(workspace_id,source_id,id,external_review_id)
                               VALUES (%s,%s,%s,%s)""", [item[:-1] for item in identities])
        else:
            cur.executemany("""INSERT INTO review_identity(workspace_id,source_id,id,external_review_id,external_place_id)
                               VALUES (%s,%s,%s,%s,%s)""", identities)
        cur.executemany("""INSERT INTO review_version
                           (workspace_id,source_id,id,identity_id,batch_id,normalization_version,
                            source_reviewer_id,external_place_id,text_content,rating,published_at,metadata,version_no)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)""", versions)
        cur.execute("""INSERT INTO snapshot(workspace_id,source_id,id,batch_id,duckdb_sha256,version_ids,manifest_sha256)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""", (workspace, source, snapshot, batch, DEMO_SHA, ids, digest))
    return summary | {"outcome": "applied"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--source", type=UUID, required=True)
    parser.add_argument("--object-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (default)")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = import_demo(args.workspace, args.source, apply=args.apply, object_root=args.object_root)
    except (OSError, ValueError, duckdb.Error, psycopg.Error):
        parser.exit(1, "Backfill refused. Verify the synthetic object, database revision and source scope.\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
