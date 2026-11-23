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

from operational.delivery_objects import DeliveryError, encoded, private_directory
from operational.import_delivery import register_bytes
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
    host = os.environ.get("PGHOST", "")
    if not host.startswith("/") or "," in host or os.getenv("PGHOSTADDR") or os.getenv("PGSERVICE"):
        raise UploadError("importer_unavailable")
    with psycopg.connect(
        user="rs_upload",
        host=host,
        connect_timeout=3,
        options="-c search_path=pg_catalog,public -c statement_timeout=90000 -c jit=off",
    ) as conn:
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


class Handler(socketserver.StreamRequestHandler):
    timeout = 20

    def handle(self):
        try:
            msg = read_json(self.rfile)
            parts = tuple(self.rfile.read(n) for n in sizes(msg["sizes"]))
            if self.rfile.read(1):
                raise UploadError("invalid_upload")
            reply = apply(msg, parts)
        except (DeliveryError, UploadError) as exc:
            reply = {
                "error": "scope_denied" if str(exc) == "scope_denied" else "importer_unavailable"
            }
        except (ValueError, OSError, TypeError, KeyError, psycopg.Error):
            reply = {"error": "importer_unavailable"}
        write_json(self.wfile, reply)


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
