"""Score only the windows referenced by a verified PR-19 artifact."""

from hashlib import sha256

from operational import candidate_worker
from operational.delivery_objects import encoded

KIND = "snapshot_rerank_v1"
SCHEMA = "candidate-rerank-1"
PROFILE = "mmarco-context-logit-v1"


def verify_parent(parent, params, summary):
    doc = parent.copy()
    result_hash = doc.pop("result_sha256")
    run = doc.pop("run_id")
    if (
        sha256(encoded(doc)).hexdigest() != result_hash
        or run != params["parent_run_id"]
        or doc["schema"] != candidate_worker.SCHEMA
        or doc["status"] != "READY"
        or doc["aspect"] != params["aspect"]
        or params["profile"] != PROFILE
        or doc["query_sha256"] != sha256(params["query"].encode()).hexdigest()
        or any(
            doc[k] != summary[k]
            for k in (
                "workspace_id",
                "source_id",
                "snapshot_id",
                "manifest_sha256",
                "snapshot_sha256",
            )
        )
    ):
        raise ValueError("INVALID_PARENT")


def compute(parent, rows, query):
    from reviewscope.candidates import Document
    from reviewscope.embeddings.rerank import FALLBACK, MODELS
    from reviewscope.embeddings.sentence_transformer import installed_runtime
    from reviewscope.rerank import reorder

    docs = {r[0]: Document(*r) for r in rows}
    try:
        scores = reorder(parent, docs, query)
    except ValueError as exc:
        if str(exc) not in FALLBACK | {"OFFLINE_REQUIRED"}:
            raise
        return dict(status="BASELINE_RETAINED", reason=str(exc), ranking=[])
    return dict(
        status="READY",
        ranking=scores,
        runtime=installed_runtime(),
        revision=MODELS["ce"]["revision"],
    )


def result(summary, rows, params, parent, pulse):
    verify_parent(parent, params, summary)
    versions = {h["version_id"] for h in parent["candidates"]}
    doc = candidate_worker.child(
        "rerank", [parent, [r for r in rows if r[0] in versions], params["query"]], pulse
    )
    doc.update(
        {
            k: parent[k]
            for k in (
                "workspace_id",
                "source_id",
                "snapshot_id",
                "manifest_sha256",
                "snapshot_sha256",
                "query_sha256",
                "aspect",
            )
        }
    )
    doc.update(
        schema=SCHEMA,
        job_kind=KIND,
        profile=PROFILE,
        run_id=summary["run_id"],
        parent_run_id=params["parent_run_id"],
        parent_sha256=params["parent_sha256"],
        parent_result_sha256=parent["result_sha256"],
    )
    doc["result_sha256"] = sha256(encoded(doc)).hexdigest()
    return doc
