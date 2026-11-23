"""Deterministic ZIP delivery into operator-verified private owner custody on Linux."""

import hashlib
import json
import os
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from operational.delivery_objects import encoded, object_directory, open_object, private_directory
from operational.import_delivery import FORMAT, verify_snapshot
from operational.retention import exclusive, one, request, require, root_binding, scope
from reviewscope.access.ledger import gate

LIMIT = 256 * 1024 * 1024
TABLES = {
    "client_project": "id",
    "location": "project_id,id",
    "source": "id",
    "source_location": "source_id,external_place_id",
    "import_batch": "source_id,id",
    "review_identity": "source_id,id",
    "review_version": "source_id,id",
    "import_row": "source_id,batch_id,position",
    "snapshot": "id",
    "snapshot_member": "snapshot_id,version_id",
    "analysis_run": "id",
    "artifact": "id",
}
RUN_FIELDS = """workspace_id,id,snapshot_id,engine_version,model_version,definition_version,
created_at,finished_at,status,output_artifact_id,job_kind,attempt,stage,processed,total,reason"""


def export(conn, rid, root, handoff):
    private_directory(handoff)
    require(not handoff.is_relative_to(root) and not root.is_relative_to(handoff), "handoff_scope")
    with exclusive(conn):
        with conn.cursor() as cur:
            gate(cur)
        q = request(conn, rid)
        w, k, t = scope(q)
        require(q["action"] == "export", "request_unavailable")
        # A completed selection is delivered once. The owner can verify/retrieve that same archive.
        name = str(rid) + ".zip"
        if q["state"] == "completed":
            require(one(conn, "SELECT retention_current_owner(%s)", (rid,)), "owner_required")
            with os.fdopen(
                os.open(handoff / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
            ) as f:
                require(
                    hashlib.file_digest(f, "sha256").hexdigest() == q["report"]["archive_sha256"],
                    "handoff_mismatch",
                )
            return q["report"]
        require(one(conn, "SELECT retention_eligible(%s)", (rid,)), "owner_required")
        root_binding(conn, w, root)
        conn.execute("SELECT set_config('rs.export_request',%s,true)", (str(rid),))
        conn.execute("SET LOCAL TIME ZONE 'UTC'")
        plan = one(conn, "SELECT retention_preview(%s,%s,%s)", scope(q))
        require(plan == q["report"], "preview_changed")
        require(
            not one(
                conn,
                """SELECT EXISTS(SELECT FROM snapshot s WHERE
            s.manifest_sha256<>snapshot_digest(s.workspace_id,s.source_id,s.batch_id,s.duckdb_sha256,s.filters,s.version_ids)
            OR s.version_ids<>ARRAY(SELECT version_id FROM snapshot_member m WHERE m.workspace_id=s.workspace_id
              AND m.snapshot_id=s.id ORDER BY version_id))""",
            ),
            "manifest_mismatch",
        )
        refs = plan["references"]
        directory = object_directory(root, w, create=False) if refs else None
        for source, batch in conn.execute(
            "SELECT source_id,batch_id FROM snapshot WHERE filters->>'format'=%s ORDER BY id",
            (FORMAT,),
        ):
            verify_snapshot(conn, (w, source), batch, directory)
        omissions = [
            "credentials_and_sessions",
            "operator_authorizations",
            "service_diagnostics",
            "backups",
            "external_downloads",
            "ml_index_cache_not_applicable",
        ]
        if one(conn, "SELECT EXISTS(SELECT FROM import_batch WHERE delivery IS NULL)"):
            omissions.append("legacy_position_journal_and_configuration_unavailable")
        manifest = {
            "version": "owner-export-1",
            "request_id": str(rid),
            "selection": [str(w), k, str(t)],
            "references": refs,
            "omitted": omissions,
            "entries": {},
        }
        total = 0
        with (
            os.fdopen(os.open(handoff, os.O_TMPFILE | os.O_RDWR, 0o600), "w+b") as spool,
            ZipFile(spool, "w", ZIP_STORED) as archive,
        ):

            def member(path, chunks):
                nonlocal total
                sha = hashlib.sha256()
                count = 0
                with archive.open(ZipInfo(path), "w") as out:
                    for chunk in chunks:
                        total += len(chunk)
                        require(total <= LIMIT, "export_limit")
                        out.write(chunk)
                        sha.update(chunk)
                        count += len(chunk)
                return {"bytes": count, "sha256": sha.hexdigest()}

            for table, order in TABLES.items():
                fields = RUN_FIELDS if table == "analysis_run" else "*"
                with conn.cursor(name="export_rows") as cur:
                    cur.itersize = 64
                    cur.execute(
                        f"SELECT row_to_json(r)::text FROM (SELECT {fields} FROM {table} ORDER BY {order}) r"
                    )
                    path = "rows/" + table + ".jsonl"
                    manifest["entries"][path] = member(
                        path, ((line + "\n").encode() for (line,) in cur)
                    )
            for sha in sorted({row[3] for row in refs}):
                with open_object(directory, sha) as f:
                    path = "objects/" + sha
                    item = member(path, iter(lambda: f.read(65536), b""))
                    require(item["sha256"] == sha, "checksum_mismatch")
                    manifest["entries"][path] = item
            raw = encoded(manifest)
            member("manifest.json", [raw])
            archive.close()
            spool.flush()
            os.fsync(spool.fileno())
            spool.seek(0)
            checksum = hashlib.file_digest(spool, "sha256").hexdigest()
            result = {
                "archive_sha256": checksum,
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": spool.seek(0, os.SEEK_END),
                "custody": "external_uncontrolled",
            }
            # O_TMPFILE has no visible partial filename, even if the exporter dies.
            fd = os.open(handoff, os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                try:
                    os.link(
                        f"/proc/self/fd/{spool.fileno()}", name, dst_dir_fd=fd, follow_symlinks=True
                    )
                except FileExistsError:
                    with os.fdopen(
                        os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), "rb"
                    ) as old:
                        require(
                            hashlib.file_digest(old, "sha256").hexdigest() == checksum,
                            "handoff_conflict",
                        )
                os.fsync(fd)
            finally:
                os.close(fd)
        conn.execute(
            "UPDATE retention_request SET state='completed',report=%s::jsonb WHERE id=%s",
            (json.dumps(result), rid),
        )
        return result
