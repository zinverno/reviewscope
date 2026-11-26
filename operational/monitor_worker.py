"""Manual monitor execution over the verified PR-22 target projection."""

import json
import platform
from importlib.metadata import version
from time import monotonic

from psycopg.types.json import Jsonb

from operational import decision_worker
from operational.delivery_objects import encoded
from reviewscope.access import PRIVATE_BINDS_SAFE
from reviewscope.access.ledger import certified_space, gate
from reviewscope.import_preview.contract import MAX_BYTES, MAX_CONFIG


def checked_parent(conn, claim, info, directory, pulse):
    from operational.worker import read_source, require

    with conn.transaction(), conn.cursor() as cur:
        certified_space(gate(cur), claim[0], info["root"])
        parent = cur.execute("SELECT monitor_parent(%s,%s,%s)", claim).fetchone()[0]
        require(parent and parent["monitor"], "ACCESS_REVOKED")
        if not parent["parent"]:
            return None
        raw = read_source(directory, parent["artifact"], 65536, pulse, keep=True)
        require(len(raw) == parent["artifact_bytes"] and json.loads(raw) == parent["manifest"])
        for field, limit in (
            ("file", MAX_BYTES),
            ("mapping", MAX_CONFIG),
            ("passport", MAX_CONFIG),
            ("coverage", MAX_CONFIG),
            ("snapshot", 64 * 1024**2),
        ):
            read_source(directory, parent[field], limit, pulse)
        return parent["manifest"]


def result(conn, claim, info, rows, directory, pulse):
    from operational.worker import require

    eligible = sum(bool(text.strip()) for _, _, text in rows)
    require(eligible == info["decision"]["eligible"])
    done = 0
    staged_bytes = 0
    start = monotonic()

    def heartbeat():
        pulse("summarize", done, eligible)

    def stage(batch):
        nonlocal done, staged_bytes
        raw = encoded(batch)
        with conn.transaction(), conn.cursor() as cur:
            certified_space(gate(cur), claim[0], info["root"])
            require(cur.execute(PRIVATE_BINDS_SAFE).fetchone()[0], "ACCESS_REVOKED")
            n = cur.execute(
                "SELECT decision_stage(%s,%s,%s,%s::jsonb)", (*claim, raw.decode())
            ).fetchone()[0]
            require(n == len(batch), "ACCESS_REVOKED")
        done += sum(x["outcome"] != "excluded_blank" for x in batch)
        staged_bytes += len(raw)
        heartbeat()

    heartbeat()
    parent = checked_parent(conn, claim, info, directory, heartbeat)
    runtime = dict(
        python=platform.python_version(),
        numpy=version("numpy"),
        sklearn=version("scikit-learn"),
        dtype="float64",
    )
    misses = []
    if parent and parent["runtime"] == runtime:
        for offset in range(0, len(rows), decision_worker.BATCH):
            batch = rows[offset : offset + decision_worker.BATCH]
            with conn.transaction(), conn.cursor() as cur:
                certified_space(gate(cur), claim[0], info["root"])
                require(cur.execute(PRIVATE_BINDS_SAFE).fetchone()[0], "ACCESS_REVOKED")
                hits = cur.execute(
                    "SELECT monitor_reuse_batch(%s,%s,%s,%s::uuid[],%s)",
                    (*claim, [row[0] for row in batch], Jsonb(runtime)),
                ).fetchone()[0]
                require(hits is not None, "ACCESS_REVOKED")
            reused = {str(v) for v in hits}
            misses.extend(row for row in batch if row[0] not in reused)
            done += sum(bool(text.strip()) for v, _, text in batch if v in reused)
            heartbeat()
    else:
        misses = rows
    reuse_s = monotonic() - start
    stats = decision_worker.run_batches(info["decision"], misses, heartbeat, stage)
    require(done == eligible and stats["runtime"] == runtime)
    doc = conn.execute("SELECT decision_manifest(%s,%s,%s)", claim).fetchone()[0]
    require(doc and doc["run_id"] == str(claim[1]))
    doc["runtime"] = stats.pop("runtime")
    print(
        json.dumps(
            dict(
                event="monitor_resources",
                **stats,
                reused=len(rows) - len(misses),
                computed=len(misses),
                verify_reuse_s=reuse_s,
                staged_bytes=staged_bytes,
            )
        ),
        flush=True,
    )
    pulse("publish", done, eligible)
    return doc
