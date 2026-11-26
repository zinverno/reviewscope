"""Private local importer for exact browser-approved synthetic deliveries."""

import hashlib
import json
import os
import signal
import socketserver
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import psycopg

from operational.delivery_objects import (
    DeliveryError,
    encoded,
    object_directory,
    open_object,
    private_directory,
)
from operational.import_delivery import register_bytes
from operational.signals import LIMIT, SCHEMA
from reviewscope.access.ledger import certified_space, publishing
from reviewscope.access.restore import sha
from reviewscope.access.upload_wire import UploadError, read_json, sizes, socket_path, write_json

UNKNOWN = encoded(
    dict(
        coverage="unknown",
        filters=None,
        mode=None,
        basis=None,
        covered_from=None,
        covered_until=None,
        received_at=None,
    )
)


def apply(message, parts):
    if (
        not isinstance(message, dict)
        or set(message)
        != {"workspace", "source", "delivery", "ticket", "fingerprint", "sizes", "checksum"}
        or not all(sha(message[k]) for k in ("ticket", "fingerprint", "checksum"))
        or sizes(message["sizes"]) != tuple(map(len, parts))
    ):
        raise UploadError("invalid_upload")
    w, s, d = (UUID(message[k]) for k in ("workspace", "source", "delivery"))
    mapping, passport, data = parts
    if hashlib.sha256(data).hexdigest() != message["checksum"]:
        raise UploadError("scope_denied")
    with connection() as conn:
        with conn.cursor() as cur, publishing(cur) as witness:
            (ready,) = conn.execute(
                "SELECT current_user=session_user AND current_user='rs_upload' AND browser_role_safe()"
            ).fetchone()
            if not ready:
                raise UploadError("importer_unavailable")
            conn.execute(
                "SELECT set_config('rs.upload_id',%s,true),set_config('rs.upload_proof',%s,true),"
                "set_config('rs.upload_workspace',%s,true)",
                (str(d), message["ticket"], str(w)),
            )
            row = conn.execute(
                "SELECT * FROM browser_claim(%s,%s,%s,%s,%s)",
                (w, s, d, message["checksum"], message["fingerprint"]),
            ).fetchone()
            if not row or not row[0]:
                raise UploadError("scope_denied")
            root, source_key, actor, fmt, _locations = row
            root = Path(root)
            certified_space(witness, w, root)
            private_directory(root)
            args = SimpleNamespace(
                workspace=w,
                source=s,
                delivery_id=d,
                format=fmt,
                object_root=root,
                apply=True,
                preview_fingerprint=message["fingerprint"],
            )
            result = register_bytes(
                conn, args, source_key, actor, data, mapping, passport, UNKNOWN, browser=True
            )
            snapshot = result["snapshot_id"]
            if not conn.execute(
                "SELECT browser_complete(%s,%s,%s,%s)", (w, s, d, snapshot)
            ).fetchone()[0]:
                raise UploadError("scope_denied")
            return {"snapshot_id": snapshot}


def connection():
    host = os.environ.get("PGHOST", "")
    if not host.startswith("/") or "," in host or os.getenv("PGHOSTADDR") or os.getenv("PGSERVICE"):
        raise UploadError("importer_unavailable")
    return psycopg.connect(
        user="rs_upload",
        host=host,
        connect_timeout=3,
        options="-c search_path=pg_catalog,public -c statement_timeout=90000 -c jit=off",
    )


def read_signals(msg):
    function, schema = {
        "read_signals": ("signal_object", SCHEMA),
        "read_candidates": ("candidate_object", "candidate-search-1"),
        "read_rerank": ("rerank_object", "candidate-rerank-1"),
        "topic_snapshot": ("topic_object", "topic-snapshot-1"),
        "relation_snapshot": ("relation_object", "relation-snapshot-1"),
        "read_decisions": ("decision_object", "aspect-decision-1"),
    }[msg["op"]]
    if set(msg) != {"op", "workspace", "source", "snapshot", "run", "actor", "expires", "ticket"}:
        raise UploadError("scope_denied")
    ids = [UUID(msg[k]) for k in ("workspace", "source", "snapshot", "run", "actor")]
    with connection() as conn, conn.cursor() as cur, publishing(cur) as witness:
        if not conn.execute(
            "SELECT browser_role_safe() AND current_user=session_user AND current_user='rs_upload'"
        ).fetchone()[0]:
            raise UploadError("importer_unavailable")
        row = conn.execute(
            f"SELECT * FROM {function}(%s,%s,%s,%s,%s,%s,%s)",
            (*ids, msg["expires"], msg["ticket"]),
        ).fetchone()
        if row is None:
            raise UploadError("scope_denied")
        root, digest, size, manifest, *extra = row
        certified_space(witness, ids[0], root)
        directory = object_directory(Path(root), ids[0], create=False)
        if msg["op"] == "topic_snapshot":
            for parent in set(extra[0]):
                with open_object(directory, parent) as blob:
                    data = blob.read(size + 1)
                    if len(data) > size or hashlib.sha256(data).hexdigest() != parent:
                        raise UploadError("importer_unavailable")
        with open_object(directory, digest) as stream:
            if msg["op"] in ("relation_snapshot", "topic_snapshot"):
                raw = stream.read(size + 1)
                if len(raw) > size or hashlib.sha256(raw).hexdigest() != digest:
                    raise UploadError("importer_unavailable")
                receipt = {"schema": schema, "snapshot_id": str(ids[2]), "manifest": manifest, "sha256": digest}
                if msg["op"] == "topic_snapshot":
                    header, *rows = raw.splitlines()
                    scope = json.loads(header)
                    if (scope["workspace"], scope["source"], scope["format"]) != (str(ids[0]), str(ids[1]), "canonical-json-v1"):
                        raise UploadError("importer_unavailable")
                    checksum = hashlib.sha256()
                    for line in rows:
                        version = json.loads(line)
                        text = version["text"]
                        text_sha = hashlib.sha256(text.encode()).hexdigest() if text is not None else "-"
                        checksum.update(f"{version['version_id']}:{version['content_sha256']}:{text_sha}\n".encode())
                    receipt["source_rows_sha256"] = checksum.hexdigest()
                return receipt
            raw = stream.read(LIMIT + 1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise UploadError("importer_unavailable")
        doc = json.loads(raw)
        if (
            doc["schema"] != schema
            or doc["manifest_sha256"] != manifest
            or [doc[k] for k in ("workspace_id", "source_id", "snapshot_id", "run_id")]
            != list(map(str, ids[:4]))
        ):
            raise UploadError("importer_unavailable")
        return doc


class Handler(socketserver.StreamRequestHandler):
    timeout = 20

    def handle(self):
        limit = 4096
        try:
            msg = read_json(self.rfile)
            signal_read = isinstance(msg, dict) and msg.get("op") in ("read_signals","read_candidates","read_rerank","read_decisions","relation_snapshot","topic_snapshot")
            parts = () if signal_read else tuple(self.rfile.read(n) for n in sizes(msg["sizes"]))
            if self.rfile.read(1):
                raise UploadError("invalid_upload")
            reply = read_signals(msg) if signal_read else apply(msg, parts)
            limit = LIMIT if signal_read else 4096
        except (DeliveryError, UploadError) as exc:
            reply = {
                "error": "scope_denied" if str(exc) == "scope_denied" else "importer_unavailable"
            }
        except (ValueError, OSError, TypeError, KeyError, psycopg.Error):
            reply = {"error": "importer_unavailable"}
        write_json(self.wfile, reply, limit)


class Server(socketserver.UnixStreamServer):
    def handle_error(self, request, client_address):
        print('{"error":"importer_unavailable"}', file=sys.stderr)


def main():
    def shutdown(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    os.umask(0o077)
    try:
        path = socket_path()
        with Server(str(path), Handler) as server:
            inode = path.stat()
            try:
                server.serve_forever()
            finally:
                if os.path.samestat(inode, path.lstat()):
                    path.unlink()
    except (OSError, ValueError):
        print(json.dumps({"error": "importer_unavailable"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
