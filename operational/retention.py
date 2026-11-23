"""Local retention operator. Secrets, archive paths and SQL exceptions never go to stdout."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from operational.delivery_objects import DeliveryError, encoded, private_directory, sync_directory
from operational.restore_copy import activate_roots, prepare
from reviewscope.access.ledger import (
    LIMIT,
    SCOPED,
    LedgerError,
    active_receipts,
    certified_space,
    digest,
    gate,
    ledger_path,
    read_witness,
)
from reviewscope.access.restore import RestoreError, mount_id

GRACE_SECONDS = 60


def require(condition, code):
    if not condition:
        raise DeliveryError(code)


def one(conn, query, params=()):
    return conn.execute(query, params).fetchone()[0]


def limited_operator(conn):
    require(
        one(
            conn,
            """SELECT current_user=session_user AND current_user='rs_retention'
      AND NOT (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication)
      AND NOT EXISTS(SELECT FROM pg_auth_members WHERE member=r.oid)
      AND NOT EXISTS(SELECT FROM pg_class WHERE relowner=r.oid)
      AND NOT EXISTS(SELECT FROM pg_database WHERE datdba=r.oid)
      AND NOT EXISTS(SELECT FROM pg_namespace WHERE nspowner=r.oid)
      AND NOT has_schema_privilege(current_user,'public','CREATE')
      AND NOT has_database_privilege(current_user,current_database(),'CREATE,TEMP')
      AND row_security_active('review_version') AND row_security_active('import_row')
      AND NOT EXISTS(SELECT FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r'
          AND has_table_privilege(current_user,oid,'DELETE,TRUNCATE'))
      FROM pg_roles r WHERE rolname=current_user""",
        ),
        "operator_role_refused",
    )


@contextmanager
def exclusive(conn):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(11713,12)")
        fd = os.open(ledger_path().parent, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def stamp(conn):
    value = one(conn, "SELECT retention_stamp()")
    return value.pop("checkpoint"), value


def save(conn, body, *, initial=False):
    path = ledger_path()
    data = {"body": body, "sha256": digest(body)}
    content = encoded(data)
    require(len(content) <= LIMIT, "ledger_capacity")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
            if initial:
                os.link(tmp, path)
            else:
                os.replace(tmp, path)
            sync_directory(path.parent)
        finally:
            tmp.unlink(missing_ok=True)
    conn.execute(
        "UPDATE retention_control SET checkpoint=%s,live_binding=%s::jsonb",
        (data["sha256"], json.dumps(data["body"]["binding"])),
    )
    return data["sha256"]


def initialize(conn):
    with exclusive(conn):
        head, binding = stamp(conn)
        require(
            head is None
            and not one(conn, "SELECT enrolled FROM retention_control")
            and not one(conn, "SELECT EXISTS(SELECT FROM retention_request)"),
            "already_enrolled",
        )
        conn.execute("UPDATE retention_control SET enrolled=true")
        return save(
            conn, {"version": 2, "binding": binding, "events": [], "receipts": []}, initial=True
        )


def request(conn, rid):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM retention_request WHERE id=%s FOR UPDATE", (rid,))
        row = cur.fetchone()
    require(row is not None, "request_unavailable")
    return row


def scope(q):
    return q["workspace_id"], q["kind"], q["target"]


def object_identity(root):
    private_directory(root)
    authority = ledger_path().parent
    require(
        not authority.is_relative_to(root) and not root.is_relative_to(authority),
        "independent_ledger_required",
    )
    info = root.stat()
    return [info.st_dev, info.st_ino]


def root_binding(conn, w, root):
    certified_space(read_witness(), w, root)
    registered = one(conn, "SELECT retention_root(%s)", (w,))
    require(registered == str(root), "object_root_unverified")
    return object_identity(root)


def revoke(conn, event, identity):
    r = event[0]
    conn.execute("SELECT revoke_retention(%s,%s,%s,%s,%s)", event)
    conn.execute("UPDATE retention_request SET device=%s,inode=%s WHERE id=%s", (*identity, r))


def confirm(conn, rid, preview_sha, root):
    with exclusive(conn):
        data = read_witness()
        head, binding = stamp(conn)
        active_receipts(data)
        require(binding == data["body"]["binding"], "restore_required")
        q = request(conn, rid)
        require(q["action"] == "erase" and preview_sha == q["preview_sha"], "confirmation_mismatch")
        event = [str(x) for x in (rid, *scope(q), q["request_key"])]
        events = data["body"]["events"]
        if event in events:
            if q["state"] in {"requested", "blocked"}:
                require(events[-1] == event, "restore_required")
                identity = root_binding(conn, q["workspace_id"], root)
                require(
                    one(conn, "SELECT approve_retention(%s,%s)", (rid, preview_sha)),
                    "preview_changed",
                )
                revoke(conn, event, identity)
                conn.execute(
                    "UPDATE retention_control SET checkpoint=%s,live_binding=%s::jsonb",
                    (data["sha256"], json.dumps(data["body"]["binding"])),
                )
            return request(conn, rid)["state"]
        require(head == data["sha256"], "restore_required")
        require(one(conn, "SELECT retention_eligible(%s)", (rid,)), "owner_required")
        if one(conn, "SELECT retention_held(%s,%s,%s)", scope(q)):
            conn.execute("UPDATE retention_request SET state='blocked' WHERE id=%s", (rid,))
            return "blocked"
        identity = root_binding(conn, q["workspace_id"], root)
        require(one(conn, "SELECT approve_retention(%s,%s)", (rid, preview_sha)), "preview_changed")
        events.append(event)
        save(conn, data["body"])
        revoke(conn, event, identity)
        return "purging"


@contextmanager
def objects(root, workspace, identity, certified=None):
    # Walk every directory by file descriptor, including ancestors of the private root.
    private_directory(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for depth, part in enumerate((*root.parts[1:], str(UUID(str(workspace))), "sha256"), 1):
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            if depth == len(root.parts) - 1:
                st = os.fstat(fd)
                require([st.st_dev, st.st_ino] == identity, "object_root_unverified")
            if part in (str(workspace), "sha256"):
                info = os.fstat(fd)
                if certified is not None:
                    expected = certified[1 if part == "sha256" else 0]
                    require(
                        [info.st_dev, info.st_ino, mount_id(fd)] == expected,
                        "object_root_unverified",
                    )
                require(
                    info.st_uid == os.getuid() and not info.st_mode & 0o077,
                    "private_store_required",
                )
        yield fd
    finally:
        os.close(fd)


def check_file(fd, name, *, hashed):
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    require(
        stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "unexpected_object",
    )
    with os.fdopen(
        os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), "rb"
    ) as stream:
        require(
            os.path.samestat(info, os.fstat(stream.fileno()))
            and info.st_nlink == 1
            and mount_id(fd) == mount_id(stream.fileno()),
            "object_changed",
        )
        if hashed:
            require(hashlib.file_digest(stream, "sha256").hexdigest() == name, "checksum_mismatch")
    return info


def purge(conn, rid, root):
    # Commit verified unlink intent first. A missing object is acceptable only on resumption.
    with exclusive(conn):
        with conn.cursor() as cur:
            evidence = gate(cur)
        q = request(conn, rid)
        certified = certified_space(evidence, q["workspace_id"], root)
        require(
            q["action"] == "erase"
            and q["state"] in {"purging", "failed", "verifying", "completed"},
            "request_unavailable",
        )
        require(object_identity(root) == [q["device"], q["inode"]], "object_root_unverified")
        if q["state"] == "completed":
            return "completed"
        pending = conn.execute(
            "SELECT digest FROM retention_object WHERE request_id=%s AND NOT verified", (rid,)
        ).fetchall()
        if pending:
            with objects(root, q["workspace_id"], [q["device"], q["inode"]], certified) as fd:
                for (name,) in pending:
                    require(re.fullmatch(r"[0-9a-f]{64}", name), "invalid_digest")
                    try:
                        check_file(fd, name, hashed=True)
                    except FileNotFoundError:
                        # Another approved purge may have already completed its durable intent.
                        proven = one(
                            conn,
                            """SELECT EXISTS(SELECT FROM retention_object o JOIN retention_request r ON r.id=o.request_id
                            WHERE r.workspace_id=%s AND r.device=%s AND r.inode=%s AND o.digest=%s
                            AND o.verified AND o.outcome='erased') AND NOT retention_referenced(%s,%s)""",
                            (
                                q["workspace_id"],
                                q["device"],
                                q["inode"],
                                name,
                                q["workspace_id"],
                                name,
                            ),
                        )
                        require(proven, "missing_object")
            conn.execute("UPDATE retention_object SET verified=true WHERE request_id=%s", (rid,))
        conn.execute("UPDATE retention_request SET state='verifying' WHERE id=%s", (rid,))
    with exclusive(conn):
        with conn.cursor() as cur:
            evidence = gate(cur)
        q = request(conn, rid)
        certified = certified_space(evidence, q["workspace_id"], root)
        require(object_identity(root) == [q["device"], q["inode"]], "object_root_unverified")
        candidates = {
            row[0]: None
            for row in conn.execute(
                "SELECT digest FROM retention_object WHERE request_id=%s", (rid,)
            )
        }
        w = q["workspace_id"]
        young = False
        orphan_count = 0
        # An empty namespace is valid only for a scope with no registered files.
        if candidates or os.path.lexists(root / str(w)):
            with objects(root, w, [q["device"], q["inode"]], certified) as fd:
                for name in os.listdir(fd):
                    hashed = re.fullmatch(r"[0-9a-f]{64}", name) is not None
                    require(hashed or re.fullmatch(r"tmp[a-z0-9_]{8}", name), "unexpected_object")
                    if hashed and one(conn, "SELECT retention_referenced(%s,%s)", (w, name)):
                        continue
                    info = check_file(fd, name, hashed=hashed)
                    reserved = (
                        hashed
                        and name not in candidates
                        and one(
                            conn,
                            """SELECT EXISTS(
                        SELECT FROM retention_object o JOIN retention_request q ON q.id=o.request_id
                        WHERE q.workspace_id=%s AND q.device=%s AND q.inode=%s AND o.digest=%s)""",
                            (w, q["device"], q["inode"], name),
                        )
                    )
                    if reserved or (
                        name not in candidates and time.time() - info.st_mtime < GRACE_SECONDS
                    ):
                        young = True
                        continue
                    os.unlink(name, dir_fd=fd)
                    orphan_count += int(name not in candidates)
                os.fsync(fd)
                remaining = set(os.listdir(fd))
                for name in candidates:
                    if one(conn, "SELECT retention_referenced(%s,%s)", (w, name)):
                        check_file(fd, name, hashed=True)
                        candidates[name] = "retained_with_reason"
                    else:
                        require(name not in remaining, "verification_failed")
                        candidates[name] = "erased"
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE retention_object SET outcome=%s WHERE request_id=%s AND digest=%s",
                ((outcome or "erased", rid, name) for name, outcome in candidates.items()),
            )
        remaining_graph = one(conn, "SELECT retention_preview(%s,%s,%s)", scope(q))
        require(
            not remaining_graph["references"] and not any(remaining_graph["tables"].values()),
            "verification_failed",
        )
        state = "purging" if young else "completed"
        by_class = {}
        for label, outcome, count in conn.execute(
            """SELECT label,outcome,count(*) FROM retention_object,
                unnest(classes) label WHERE request_id=%s GROUP BY label,outcome""",
            (rid,),
        ):
            by_class.setdefault(label, {})[outcome] = count
        report = {
            "tables": (q["report"] or {}).get("tables", {}),
            "physical_objects_by_class": by_class,
            "erased": sum(v != "retained_with_reason" for v in candidates.values()),
            "shared": sum(v == "retained_with_reason" for v in candidates.values()),
            "orphan_files_removed_this_pass": orphan_count,
            "grace_pending": young,
            "backup": "BACKUP_ERASURE_NOT_VERIFIED",
            "downloads": "external_uncontrolled",
            "index_cache": "not_applicable",
        }
        conn.execute(
            "UPDATE retention_request SET state=%s, report=%s::jsonb WHERE id=%s",
            (state, json.dumps(report), rid),
        )
        if state == "completed":
            conn.execute("SELECT finish_retention(%s)", (rid,))
        return state


def access_seal(conn):
    return one(conn, "SELECT retention_access_seal()")


def verify_catalog(conn):
    broken = one(
        conn,
        """SELECT EXISTS(SELECT FROM snapshot s WHERE s.manifest_sha256 <>
        snapshot_digest(s.workspace_id,s.source_id,s.batch_id,s.duckdb_sha256,s.filters,s.version_ids) OR
        s.version_ids <> ARRAY(SELECT version_id FROM snapshot_member m WHERE (m.workspace_id,m.snapshot_id)=(s.workspace_id,s.id) ORDER BY version_id))
        OR EXISTS (SELECT FROM import_batch b LEFT JOIN snapshot s
          ON (s.workspace_id,s.source_id,s.batch_id)=(b.workspace_id,b.source_id,b.id) AND s.filters->>'format'='canonical-json-v1'
          CROSS JOIN LATERAL (SELECT count(*) n,count(*) FILTER(WHERE status='quarantined') q,
            count(*) FILTER(WHERE status='confirmed_duplicate') d,
            coalesce(array_agg(version_id ORDER BY version_id) FILTER(WHERE status='accepted'),'{}') ids
            FROM import_row WHERE (workspace_id,source_id,batch_id)=(b.workspace_id,b.source_id,b.id)) j
          WHERE b.delivery IS NOT NULL AND (s.id IS NULL OR (b.input_count,b.rejected_count,b.confirmed_duplicate_count,b.accepted_count,s.version_ids)
          IS DISTINCT FROM (j.n,j.q,j.d,cardinality(j.ids),j.ids)))""",
    )
    require(not broken, "manifest_mismatch")


def verify_restore_roles(admin):
    allowed = admin.execute(
        """SELECT count(*)=3 AND bool_and(NOT (rolsuper OR rolbypassrls OR rolcreatedb
            OR rolcreaterole OR rolreplication) AND NOT EXISTS
            (SELECT FROM pg_auth_members WHERE member=r.oid) AND NOT EXISTS
            (SELECT FROM pg_class WHERE relowner=r.oid) AND NOT EXISTS
            (SELECT FROM pg_namespace WHERE nspowner=r.oid) AND NOT EXISTS
            (SELECT FROM pg_database WHERE datdba=r.oid)
            AND NOT has_schema_privilege(r.oid,'public','CREATE')
            AND NOT has_database_privilege(r.oid,current_database(),'CREATE,TEMP')
            AND NOT EXISTS(SELECT FROM pg_class WHERE relnamespace='public'::regnamespace
                AND relkind='r' AND has_table_privilege(r.oid,oid,'DELETE,TRUNCATE'))
            AND (rolname='rs_retention' OR NOT EXISTS(SELECT FROM pg_class c
                WHERE relnamespace='public'::regnamespace AND relkind='r'
                AND has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE')))
            AND (rolname='rs_retention' OR NOT (has_any_column_privilege(r.oid,'import_row','SELECT')
                OR has_any_column_privilege(r.oid,'review_version','SELECT'))))
            AND (SELECT bool_and(relrowsecurity) AND count(*)=cardinality(%s::text[])
                FROM pg_class WHERE relnamespace='public'::regnamespace AND relname=ANY(%s))
            FROM pg_roles r WHERE rolname IN ('rs_runtime','rs_worker','rs_retention')""",
        (SCOPED, SCOPED),
    ).fetchone()[0]
    require(allowed, "restore_roles_unverified")


def recover(admin, expected, receipts, access_sha):
    """Offline activation. Operator attests the head from the independent live authority."""
    require(admin.autocommit, "offline_connection_required")
    with exclusive(admin):
        data = read_witness()
        checkpoint, binding = stamp(admin)
        require(
            data["sha256"] == expected
            and binding["installation"] == data["body"]["binding"]["installation"],
            "ledger_unverified",
        )
        prior = one(admin, "SELECT recovery_binding FROM retention_control")
        require(isinstance(receipts, (list, tuple)) and receipts, "restore_receipt_required")
        already_sealed = (
            data["body"].get("receipts") == sorted(receipts) and binding == data["body"]["binding"]
        )
        verify_restore_roles(admin)
        if already_sealed and checkpoint == expected:
            require(access_sha == access_seal(admin), "access_review_required")
            active_receipts(data)
            return expected
        require(
            binding != data["body"]["binding"] or already_sealed and checkpoint is None,
            "restore_target_unverified",
        )
        roots = activate_roots(receipts, binding, data, access_sha, resume_seal=already_sealed)
        # An older PostgreSQL image can never authorize a fresh empty ledger.
        # Reject a genuine but old witness if this database has later completed prohibitions.
        events = data["body"]["events"]
        known = {
            str(r)
            for (r,) in admin.execute(
                "SELECT id FROM retention_request WHERE action='erase' AND state IN ('revoking','purging','verifying','failed','completed')"
            )
        }
        require(known <= {e[0] for e in events}, "ledger_behind")
        # Persistently close the DB before staged replay, including if a later step fails.
        admin.execute("UPDATE retention_control SET checkpoint=NULL")
        admin.execute(
            "UPDATE access_session SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL"
        )
        admin.execute("""UPDATE analysis_run SET status='cancelled',cancel_requested=true,reason='CANCELLED',
            lease_until=NULL,lease_token=NULL,ready_at=NULL,finished_at=clock_timestamp()
            WHERE request_key IS NOT NULL AND status IN ('queued','running')""")
        admin.execute(
            "UPDATE retention_request SET state='revoked',report=NULL WHERE action='export'"
        )
        stored = dict(
            admin.execute(
                "SELECT id,delivery_object_root FROM workspace WHERE delivery_object_root IS NOT NULL"
            ).fetchall()
        )
        required = (
            set(stored)
            | {UUID(e[1]) for e in events}
            | {w for (w,) in admin.execute("SELECT DISTINCT workspace_id FROM retention_refs")}
        )
        require(required == set(roots), "restore_roots_incomplete")
        identities = {w: object_identity(path) for w, path in roots.items()}
        recovery_id = digest(
            [
                binding,
                sorted(receipts),
                sorted((str(w), str(path), identities[w]) for w, path in roots.items()),
            ]
        )
        # Replay may remove workspace rows. Pin the checked destinations before replay,
        # and install them only in the transaction that reopens service.
        pending = "pending-receipt-v1:" + recovery_id
        if checkpoint is None:
            require(prior == pending, "unverified_recovery_roots")
        else:
            for w, path in roots.items():
                original = Path(stored[w]) if w in stored else None
                if binding != data["body"]["binding"] and original is not None:
                    require(path != original, "independent_objects_required")
                    if original.exists():
                        require(not path.samefile(original), "independent_objects_required")
        for w, name in admin.execute("SELECT DISTINCT workspace_id,digest FROM retention_refs"):
            require(isinstance(name, str) and re.fullmatch("[0-9a-f]{64}", name), "invalid_digest")
            with objects(roots[w], w, identities[w]) as fd:
                check_file(fd, name, hashed=True)
        admin.execute("UPDATE retention_control SET recovery_binding=%s", (pending,))
        for r, w, k, t, key in events:
            admin.execute(
                """INSERT INTO retention_request(id,workspace_id,kind,target,request_key,action)
                VALUES(%s,%s,%s,%s,%s,'erase') ON CONFLICT(id) DO NOTHING""",
                (r, w, k, t, key),
            )
            admin.execute(
                "UPDATE retention_request SET approved_at=clock_timestamp() WHERE id=%s", (r,)
            )
    with psycopg.connect(user="rs_retention", dbname=admin.info.dbname, autocommit=True) as op:
        limited_operator(op)
        with exclusive(op):
            require(read_witness()["sha256"] == expected, "ledger_changed")
            require(
                one(op, "SELECT recovery_binding FROM retention_control") == pending,
                "unverified_recovery_roots",
            )
            for event in events:
                revoke(op, event, identities[UUID(event[1])])
    with exclusive(admin):
        from operational.delivery_objects import object_directory
        from operational.import_delivery import FORMAT, verify_snapshot

        require(read_witness()["sha256"] == expected, "ledger_changed")
        require(access_sha == access_seal(admin), "access_review_required")
        for w, root in roots.items():
            refs = admin.execute(
                "SELECT DISTINCT digest FROM retention_refs WHERE workspace_id=%s", (w,)
            ).fetchall()
            if refs:
                with objects(root, w, identities[w]) as fd:
                    for (name,) in refs:
                        require(
                            isinstance(name, str) and re.fullmatch(r"[0-9a-f]{64}", name),
                            "invalid_digest",
                        )
                        check_file(fd, name, hashed=True)
            for source, batch in admin.execute(
                "SELECT source_id,batch_id FROM snapshot WHERE workspace_id=%s AND filters->>'format'=%s",
                (w, FORMAT),
            ):
                verify_snapshot(admin, (w, source), batch, object_directory(root, w, create=False))
        verify_catalog(admin)
        verify_restore_roles(admin)
        activate_roots(receipts, binding, data, access_sha, resume_seal=already_sealed)
        require(
            one(admin, "SELECT recovery_binding FROM retention_control") == pending,
            "unverified_recovery_roots",
        )
        for w, path in roots.items():
            require(object_identity(path) == identities[w], "unverified_recovery_roots")
            admin.execute(
                "UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (str(path), w)
            )
        admin.execute(
            "UPDATE retention_control SET enrolled=true,recovery_binding=%s", (recovery_id,)
        )
        data["body"].update(version=2, binding=binding, receipts=sorted(receipts))
        return save(admin, data["body"])


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise DeliveryError("invalid_arguments")


def main(argv=None):
    parser = Arguments(allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("initialize")
    sub.add_parser("access-seal")
    recovery = sub.add_parser("recover")
    recovery.add_argument("--latest-sha256", required=True)
    recovery.add_argument("--access-sha256", required=True)
    recovery.add_argument("--receipt", action="append", required=True)
    copy = sub.add_parser("prepare-restore")
    copy.add_argument("--erased-workspace", action="append", type=UUID, default=[])
    for arg in ("source", "destination", "inventory"):
        copy.add_argument("--" + arg, type=Path, required=True)
    for arg in ("inventory-sha256", "latest-sha256", "access-sha256"):
        copy.add_argument("--" + arg, required=True)
    for cmd in ("confirm", "purge", "export"):
        child = sub.add_parser(cmd)
        child.add_argument("--request", type=UUID, required=True)
        child.add_argument("--root", type=Path, required=True)
        if cmd == "confirm":
            child.add_argument("--preview-sha256", required=True)
        if cmd == "export":
            child.add_argument("--owner-handoff", type=Path, required=True)
    try:
        started = time.monotonic()
        args = parser.parse_args(argv)
        host = os.environ.get("PGHOST", "")
        require(
            host.startswith("/")
            and "," not in host
            and not os.getenv("PGHOSTADDR")
            and not os.getenv("PGSERVICE"),
            "local_socket_required",
        )
        role = (
            {}
            if args.command in {"initialize", "recover", "prepare-restore"}
            else {"user": "rs_retention"}
        )
        with psycopg.connect(
            **role, host=host, autocommit=True, options="-c search_path=pg_catalog,public"
        ) as conn:
            if role:
                limited_operator(conn)
            if args.command == "initialize":
                result = initialize(conn)
            elif args.command == "recover":
                result = recover(conn, args.latest_sha256, args.receipt, args.access_sha256)
            elif args.command == "prepare-restore":
                result = prepare(
                    conn,
                    args.source,
                    args.destination,
                    args.inventory,
                    args.inventory_sha256,
                    args.latest_sha256,
                    args.access_sha256,
                    args.erased_workspace,
                )
            elif args.command == "access-seal":
                with conn.transaction(), conn.cursor() as cur:
                    gate(cur)
                    result = access_seal(conn)
            elif args.command == "confirm":
                result = confirm(conn, args.request, args.preview_sha256, args.root)
            elif args.command == "purge":
                try:
                    result = purge(conn, args.request, args.root)
                except (OSError, psycopg.Error, DeliveryError, LedgerError):
                    with conn.transaction(), conn.cursor() as cur:
                        gate(cur)
                        cur.execute(
                            "UPDATE retention_request SET state='failed' WHERE id=%s AND action='erase' AND state IN ('verifying','purging')",
                            (args.request,),
                        )
                    raise
            else:
                from operational.retention_export import export

                result = export(conn, args.request, args.root, args.owner_handoff)
            from operational.worker import linux_peak_kib

            metrics = {
                "operator_peak_kib": linux_peak_kib("self"),
                "db_backend_peak_kib": linux_peak_kib(conn.info.backend_pid),
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        print(json.dumps({"result": result, "metrics": metrics}))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError, psycopg.Error) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc)
                    if isinstance(exc, (DeliveryError, LedgerError, RestoreError))
                    else "operation_refused"
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
