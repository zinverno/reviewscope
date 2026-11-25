"""Isolated full-snapshot inference with one supervised process and fenced row batches."""

import ctypes
import hashlib
import json
import os
import platform
import signal
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from importlib.metadata import version
from time import monotonic, process_time

from operational import candidate_worker
from operational.delivery_objects import encoded

KIND = "snapshot_aspect_decision_v1"
SCHEMA = "aspect-decision-1"
PROFILE = "aspect-linear-lexical-v1"
THRESHOLDS = "calibrated-v1"
BATCH = 32
FAILURES = {"MODEL_UNAVAILABLE", "UNSUPPORTED_DEFINITION", "INVALID_PROFILE", "RESOURCE_LIMIT"}


def score_batches(params, texts):
    from operational.worker import linux_peak_kib
    from reviewscope.aspect_decisions import DecisionScorer

    if {name for _, name in socket.if_nameindex()} - {"lo"}:
        raise ValueError("MODEL_UNAVAILABLE")
    if params["profile"] != PROFILE or params["threshold_set"] != THRESHOLDS:
        raise ValueError("INVALID_PROFILE")
    started = monotonic()
    scorer = DecisionScorer(params, PROFILE)
    if scorer.profile_sha256 != params["profile_sha256"] or scorer.threshold_set != THRESHOLDS:
        raise ValueError("INVALID_PROFILE")
    loaded = monotonic()
    scoring_s = 0.0
    cache = {}
    for offset in range(0, len(texts), BATCH):
        tick = monotonic()
        batch = []
        for text in texts[offset : offset + BATCH]:
            if not text.strip():
                result = dict(
                    outcome="excluded_blank",
                    start=None,
                    end=None,
                    score=None,
                    reason="BLANK",
                    counter_start=None,
                    counter_end=None,
                )
            else:
                if text not in cache:
                    cache[text] = scorer.score(text)
                result = cache[text]
            batch.append(result)
        scoring_s += monotonic() - tick
        yield batch
    yield dict(
        runtime=dict(
            python=platform.python_version(),
            numpy=version("numpy"),
            sklearn=version("scikit-learn"),
            dtype="float64",
        ),
        rows=len(texts),
        unique_scored=len(cache),
        load_s=loaded - started,
        score_wall_s=scoring_s,
        cpu_s=process_time(),
        **scorer.resources,
        peak_rss_kib=linux_peak_kib("self"),
    )


def run_batches(params, rows, pulse, consume):
    """Consume each bounded reply while the next heartbeat can still interrupt stalled I/O."""
    from operational.worker import JobError, linux_peak_kib, require

    started = monotonic()
    deadline = started + 120
    with (
        subprocess.Popen(
            [sys.executable, "-m", "operational.decision_worker", str(os.getpid())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=candidate_worker.offline_environment(),
        ) as child,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):

        def checked(task):
            while True:
                if monotonic() >= deadline or (linux_peak_kib(child.pid) or 0) > 2 * 1024**2:
                    raise JobError("RESOURCE_LIMIT")
                try:
                    return task.result(timeout=min(1, max(0.001, deadline - monotonic())))
                except TimeoutError:
                    pulse()

        def write_input():
            with child.stdin:
                child.stdin.write(encoded([params, [r[2] for r in rows]]))

        def read():
            line = checked(pool.submit(child.stdout.readline, candidate_worker.MAX_RESULT + 1))
            require(0 < len(line) <= candidate_worker.MAX_RESULT and line.endswith(b"\n"))
            value = json.loads(line)
            if isinstance(value, dict) and value.get("error") in FAILURES:
                raise JobError(value["error"])
            return value

        try:
            checked(pool.submit(write_input))
            for start in range(0, len(rows), BATCH):
                batch = rows[start : start + BATCH]
                scores = read()
                require(isinstance(scores, list) and len(scores) == len(batch))
                result = []
                for (version, content_sha, text), score in zip(batch, scores, strict=True):
                    require(
                        isinstance(score, dict)
                        and set(score)
                        == {
                            "outcome",
                            "start",
                            "end",
                            "score",
                            "reason",
                            "counter_start",
                            "counter_end",
                        }
                    )
                    result.append(
                        dict(
                            **score,
                            version_id=version,
                            content_sha256=content_sha,
                            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                        )
                    )
                consume(result)
            stats = read()
            require(stats["rows"] == len(rows))
            require(checked(pool.submit(child.wait)) == 0)
            require(
                monotonic() < deadline and stats["peak_rss_kib"] <= 2 * 1024**2, "RESOURCE_LIMIT"
            )
            return stats
        finally:
            if child.poll() is None:
                child.kill()


def result(conn, claim, info, rows, pulse):
    from operational.worker import require
    from reviewscope.access import PRIVATE_BINDS_SAFE
    from reviewscope.access.ledger import certified_space, gate

    eligible = sum(bool(r[2].strip()) for r in rows)
    require(eligible == info["decision"]["eligible"])
    parent_cpu_start = process_time()
    done = 0
    staged_bytes = 0
    stage_s = 0.0

    def heartbeat():
        pulse("summarize", done, eligible)

    def stage(batch):
        nonlocal done, staged_bytes, stage_s
        tick = monotonic()
        data = encoded(batch)
        with conn.transaction(), conn.cursor() as cur:
            certified_space(gate(cur), claim[0], info["root"])
            require(cur.execute(PRIVATE_BINDS_SAFE).fetchone()[0], "ACCESS_REVOKED")
            size = cur.execute(
                "SELECT decision_stage(%s,%s,%s,%s::jsonb)", (*claim, data.decode())
            ).fetchone()[0]
            require(size == len(batch), "ACCESS_REVOKED")
        done += sum(r["outcome"] != "excluded_blank" for r in batch)
        heartbeat()
        staged_bytes += len(data)
        stage_s += monotonic() - tick

    heartbeat()
    stats = run_batches(info["decision"], rows, heartbeat, stage)
    require(done == eligible)
    doc = conn.execute("SELECT decision_manifest(%s,%s,%s)", claim).fetchone()[0]
    require(isinstance(doc, dict) and doc.get("run_id") == str(claim[1]))
    doc["runtime"] = stats.pop("runtime")
    print(
        json.dumps(
            dict(
                event="decision_resources",
                **stats,
                stage_s=stage_s,
                staged_bytes=staged_bytes,
                parent_compute_cpu_s=process_time() - parent_cpu_start,
            )
        ),
        flush=True,
    )
    pulse("publish", done, eligible)
    return doc


if __name__ == "__main__":
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != int(sys.argv[1]):
        sys.exit(1)
    try:
        for message in score_batches(*json.load(sys.stdin)):
            raw = encoded(message) + b"\n"
            if len(raw) > candidate_worker.MAX_RESULT:
                raise ValueError("RESOURCE_LIMIT")
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
    except (ImportError, FileNotFoundError):
        print(json.dumps({"error": "MODEL_UNAVAILABLE"}), flush=True)
        sys.exit(1)
    except ValueError as exc:
        reason = {
            "INCOMPATIBLE_MODEL": "MODEL_UNAVAILABLE",
            "UNSUPPORTED_PROFILE": "INVALID_PROFILE",
        }.get(str(exc), str(exc))
        if reason in FAILURES:
            print(json.dumps({"error": reason}), flush=True)
        sys.exit(1)
