"""Lease-supervised offline CPU computation. Private inputs travel only through anonymous pipes."""

import ctypes
import hashlib
import json
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from time import monotonic

from operational.delivery_objects import encoded

KIND = "snapshot_candidate_search_v1"
SCHEMA = "candidate-search-1"
MAX_RESULT = 65536
MODEL_STATES = {
    "NOT_RUN_MISSING_WEIGHTS",
    "NOT_RUN_MISSING_BACKEND",
    "INCOMPATIBLE_ENCODING",
    "QUERY_TOKEN_LIMIT",
}


def compute(scope, rows, params):
    from operational.worker import linux_peak_kib
    from reviewscope.candidates import CandidateIndex, Document, text_sha
    from reviewscope.embeddings.candidate_minilm import offline_encoder

    mode, role, k, query = (params[key] for key in ("mode", "role", "k", "query"))
    try:
        started = monotonic()
        encoder = offline_encoder() if mode != "lexical" else None
        load_s = monotonic() - started
        index = CandidateIndex(scope, [Document(*r) for r in rows], mode, encoder)
        searched = monotonic()
        result = index.search(query, k, role)
        return dict(
            status="READY",
            **result,
            diagnostics=dict(
                **index.diagnostics,
                **index.query_timings,
                model_load_s=load_s,
                query_s=monotonic() - searched,
                peak_rss_kib=linux_peak_kib("self"),
            ),
        )
    except ValueError as exc:
        if str(exc) not in MODEL_STATES:
            raise
        return dict(
            status=str(exc),
            scope=scope,
            query_sha256=text_sha(query),
            mode=mode,
            role=role,
            k=k,
            reviewed_count=len(rows),
            eligible_review_count=sum(bool(r[2].strip()) for r in rows),
            fragment_count=None,
            candidate_count=0,
            retrieved_review_count=0,
            candidates=[],
        )


def result(summary, rows, params, pulse):
    from operational.worker import JobError, linux_peak_kib

    scope = {
        key: summary[key]
        for key in (
            "workspace_id",
            "source_id",
            "snapshot_id",
            "batch_id",
            "manifest_sha256",
            "snapshot_sha256",
            "coverage",
        )
    }
    env = {
        k: os.environ[k]
        for k in ("PATH", "HOME", "PYTHONPATH", "HF_HOME", "HF_HUB_CACHE", "TMPDIR")
        if k in os.environ
    }
    env.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        HF_HUB_DISABLE_PROGRESS_BARS="1",
        TOKENIZERS_PARALLELISM="false",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
    )
    deadline = monotonic() + 120
    with subprocess.Popen(
        [sys.executable, "-m", "operational.candidate_worker", str(os.getpid())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    ) as proc:
        with ThreadPoolExecutor(max_workers=1) as pool:
            reply = pool.submit(proc.communicate, encoded([scope, rows, params]))
            try:
                while True:
                    try:
                        raw, _ = reply.result(timeout=1)
                        break
                    except TimeoutError:
                        pulse()
                        if monotonic() >= deadline or (linux_peak_kib(proc.pid) or 0) > 2 * 1024**2:
                            raise JobError("RESOURCE_LIMIT") from None
            finally:
                if proc.poll() is None:
                    proc.kill()
    if proc.returncode == 3:
        raise JobError("RESOURCE_LIMIT")
    if proc.returncode != 0 or len(raw) > MAX_RESULT:
        raise JobError("INTEGRITY_ERROR")
    doc = json.loads(raw)
    diagnostics = doc.pop("diagnostics", {})
    doc.update(
        **scope,
        schema=SCHEMA,
        job_kind=KIND,
        source_sha256=summary["source_sha256"],
        observations=summary["observations"],
        quarantined=summary["quarantined"],
        confirmed_duplicates=summary["confirmed_duplicates"],
    )
    doc["result_sha256"] = hashlib.sha256(encoded(doc)).hexdigest()
    if diagnostics:
        print(json.dumps(dict(event="candidate_resources", **diagnostics)), flush=True)
    doc["run_id"] = summary["run_id"]
    return doc


if __name__ == "__main__":
    # Stop private computation if the lease-owning worker dies, including during model loading.
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != int(sys.argv[1]):
        sys.exit(1)
    try:
        payload = encoded(compute(*json.load(sys.stdin)))
        if len(payload) > MAX_RESULT:
            sys.exit(3)
        sys.stdout.buffer.write(payload)
    except ValueError as exc:
        sys.exit(3 if str(exc) == "RESOURCE_LIMIT" else 1)
