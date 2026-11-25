"""Summarize the frozen synthetic benchmark and validate matched pools and spans."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from statistics import mean

from reviewscope.validation.retrieval_dataset import digest
from scripts.benchmark_rerank import PHASES, frozen


def summarize(source):
    protocol, rows = frozen()
    summary = {}
    raw = {}
    for split in ("dev", "test"):
        phases = {g: json.loads((source / f"{split}-{g}.json").read_text()) for g in PHASES}
        raw[split] = phases
        methods = {k: m for p in phases.values() for k, m in p.get("methods", {}).items()}
        queries = [q for q in protocol["queries"] if q["split"] == split]
        scopes = {
            k: digest(m["offsets"])
            for k, m in methods.items()
            if "offsets" in m and not k.startswith("whole")
        }
        result = summary[split] = dict(
            offset_sets=scopes,
            common_offsets=len(set(scopes.values())) == 1,
            documents=sum(r["split"] == split for r in rows),
            judged_pairs=sum(
                r["labels"].get(q["aspect"], {}) is not None
                for q in queries
                for r in rows
                if r["split"] == split
            ),
            methods={},
            paired={},
            resources={
                k: {v: p.get(v) for v in ("status", "load_s", "cpu_s", "process")}
                for k, p in phases.items()
            },
        )
        for method in protocol["methods"]:
            if method not in methods:
                result["methods"][method] = None
                continue
            qdata = methods[method]["queries"]
            result["methods"][method] = {}
            for role in ("query_to_passage", "text_to_text"):
                group = [q["id"] for q in queries if q["role"] == role]
                mode = {}
                for budget in ("10", "50"):
                    samples = [qdata[q][budget] for q in group]

                    def macro(k, key, samples=samples):
                        v = [s["metrics"][k][key] for s in samples if s["metrics"][k]["eligible"]]
                        return mean(v) if v else None

                    mode[budget] = dict(
                        queries=len(samples),
                        eligible=sum(s["metrics"]["5"]["eligible"] for s in samples),
                        ndcg5=macro("5", "ndcg"),
                        recall5=macro("5", "recall"),
                        recall10=macro("10", "recall"),
                        pool_recall=macro(budget, "recall"),
                        mrr5=macro("5", "mrr"),
                        precision5=macro("5", "precision"),
                        topic5=macro("5", "topic"),
                        denial_unclear5=sum(s["metrics"]["5"]["denial_unclear"] for s in samples),
                        returned5=sum(s["metrics"]["5"]["n"] for s in samples),
                        anchors=sum(s["metrics"]["5"]["anchor_hits"] for s in samples),
                        contexts=sum(s["metrics"]["5"]["context_hits"] for s in samples),
                        spans=sum(s["metrics"]["5"]["span_total"] for s in samples),
                    )
                result["methods"][method][role] = mode
            if method.startswith("rerank_"):
                baseline = methods[method.replace("rerank_", "hybrid_")]["queries"]
                per_query = {}
                for q in queries:
                    changes = {}
                    for budget in ("10", "50"):
                        a, b = baseline[q["id"]][budget], qdata[q["id"]][budget]
                        before = {h["fragment_id"]: h for h in a["hits"]}
                        if before != {
                            h["fragment_id"]: {k: v for k, v in h.items() if k != "rerank"}
                            for h in b["hits"]
                        } or len(before) != len(b["hits"]):
                            raise ValueError("POOL_CHANGED")
                        changes[budget] = {
                            f"{metric}@{k}": b["metrics"][k][metric] - a["metrics"][k][metric]
                            for k, metric in (("5", "ndcg"), ("10", "recall"), (budget, "recall"))
                            if a["metrics"][k][metric] is not None
                        }
                    per_query[q["id"]] = changes
                result["paired"][method] = per_query
    summary["aspect_digests"] = {a["id"]: digest(a) for a in protocol["aspects"]}
    return summary, raw


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    summary, raw = summarize(args.source)
    args.output.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        raw, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode()
    compressed = gzip.compress(content, mtime=0)
    (args.output / "synthetic-matrix.json.gz").write_bytes(compressed)
    summary["uncompressed_sha256"] = hashlib.sha256(content).hexdigest()
    summary["matrix_sha256"] = hashlib.sha256(compressed).hexdigest()
    (args.output / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2, allow_nan=False) + "\n"
    )


if __name__ == "__main__":
    main()
