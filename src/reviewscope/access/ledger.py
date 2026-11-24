"""Read-only witness outside PostgreSQL and its object backups (local operator profile)."""

import errno
import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from .restore import NONCE, RestoreError, receipt, sha

SCOPED = """workspace workspace_membership client_project location source source_location import_batch
review_identity review_version snapshot snapshot_member analysis_run artifact membership_event import_row delivery_authorization
retention_policy retention_request retention_event retention_object""".split()

LIMIT = 4 * 1024 * 1024


class LedgerError(ValueError):
    def __init__(self):
        super().__init__("retention_gate_closed")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def ledger_path():
    path = Path(os.environ.get("RS_RETENTION_LEDGER", ""))
    if not path.is_absolute() or path.parent != path.parent.resolve(strict=True):
        raise LedgerError()
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LedgerError()
    return path


def read_witness():
    try:
        with os.fdopen(
            os.open(ledger_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
        ) as f:
            info = os.fstat(f.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise LedgerError()
            data = json.loads(f.read(LIMIT + 1)) if info.st_size <= LIMIT else None
        if (
            not isinstance(data, dict)
            or digest(data["body"]) != data["sha256"]
            or data["body"]["version"] not in (1, 2)
        ):
            raise LedgerError()
        body = data["body"]
        if (
            set(body)
            != (
                {"version", "events", "binding", "receipts"}
                if body["version"] == 2
                else {"version", "events", "binding"}
            )
            or set(body["binding"]) != {"installation", "database", "boot", "oid"}
            or not isinstance(body["events"], list)
        ):
            raise LedgerError()
        if body["version"] == 2:
            keys = body["receipts"]
            if (
                not isinstance(keys, list)
                or len(keys) > 256
                or not all(sha(k) for k in keys)
                or sorted(set(keys)) != keys
            ):
                raise LedgerError()
        seen = set()
        for event in body["events"]:
            request, workspace, kind, target, key = event
            if (
                kind not in {"source", "project", "workspace"}
                or (kind == "workspace" and target != workspace)
                or request in seen
            ):
                raise LedgerError()
            if any(
                not isinstance(item, str) or str(UUID(item)) != item
                for item in (request, workspace, target, key)
            ):
                raise LedgerError()
            seen.add(request)
        return data
    except (OSError, KeyError, TypeError, ValueError, RecursionError):
        raise LedgerError() from None


def active_receipts(data):
    try:
        body = data["body"]
        if body["version"] != 2:
            raise LedgerError()
        docs = [receipt(ledger_path().parent, k) for k in body["receipts"]]
        if any(doc["target"] != body["binding"] for doc in docs):
            raise LedgerError()
        return docs
    except (OSError, RestoreError):
        raise LedgerError() from None


def certified_space(data, workspace, root):
    docs = active_receipts(data)
    if not docs:
        # The previous live DB cannot adopt a generation still quarantined for replay.
        try:
            os.getxattr(root, NONCE, follow_symlinks=False)
        except OSError as exc:
            if exc.errno in (errno.ENODATA, errno.ENOTSUP):
                return None
        raise LedgerError()
    matches = [doc for doc in docs if str(workspace) in doc["spaces"] and str(root) == doc["root"]]
    if len(matches) != 1:
        raise LedgerError()
    return matches[0]["spaces"][str(workspace)]


def gate(cur):
    # Two statements matter at READ COMMITTED: acquire the lock before taking the witness snapshot.
    cur.execute("SELECT pg_advisory_xact_lock_shared(11713,12)")
    cur.execute("SELECT retention_stamp() AS stamp")
    row = cur.fetchone()
    stamp = row["stamp"] if isinstance(row, dict) else row[0]
    data = read_witness()
    if stamp.pop("checkpoint") != data["sha256"] or stamp != data["body"]["binding"]:
        raise LedgerError()
    active_receipts(data)
    return data


@contextmanager
def publishing(cur):
    # The filesystem lock survives loss of the DB connection during write/link/fsync.
    cur.execute("SELECT pg_advisory_xact_lock_shared(11713,12)")
    fd = os.open(ledger_path().parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield gate(cur)
    finally:
        os.close(fd)
