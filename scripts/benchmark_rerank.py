"""Frozen authored PR-21 benchmark. Each phase runs locally in a separate CPU process."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from time import monotonic, process_time, sleep

from reviewscope.candidates import CandidateIndex, Document, E5CandidateIndex, text_sha
from reviewscope.embeddings.rerank import E5, FALLBACK, CrossEncoder
from reviewscope.rerank import reorder
from reviewscope.validation.retrieval_dataset import digest
from reviewscope.validation.retrieval_metrics import dcg

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/data/fixtures"
KS = (1, 3, 5, 10, 20, 50)
PHASES = ("lexical", "minilm", "e5", "ce-minilm", "ce-e5")


def frozen():
    for line in (FIX / "rerank-freeze.sha256").read_text().splitlines():
        sha, name = line.split()
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != sha:
            raise ValueError("INVALID_FREEZE")
    protocol = json.loads((FIX / "rerank_protocol_v1.json").read_text())
    rows = [json.loads(line) for line in (FIX / "rerank_reviews_v1.jsonl").read_text().splitlines()]
    seen = set()
    for split in ("dev", "test"):
        texts = {r["text"].strip().casefold() for r in rows if r["split"] == split}
        families = {r["family"] for r in rows if r["split"] == split}
        if seen & (texts | families):
            raise ValueError("SPLIT_LEAK")
        seen.update(texts | families)
    return protocol, rows


def evaluate(hits, rows, aspect):
    labels = {
        r["id"]: r["labels"].get(aspect, dict(grade=0, stance="off_topic", spans=[])) for r in rows
    }
    grades = {k: (v or {}).get("grade") for k, v in labels.items()}
    relevant = {k for k, v in grades.items() if v == 2}
    ideal = sorted((v for v in grades.values() if v is not None), reverse=True)
    unknown = len(rows) - len(ideal)
    ids = list(dict.fromkeys(h["version_id"] for h in hits))
    out = {}
    for k in KS:
        top = ids[:k]
        n = len(top)
        exact = [i for i, vid in enumerate(top, 1) if vid in relevant]
        first = {vid: next(h for h in hits if h["version_id"] == vid) for vid in top}
        anchors = contexts = spans = 0
        for vid in relevant:
            for a, b, c, d in labels[vid]["spans"]:
                spans += 1
                if vid in first:
                    hit = first[vid]
                    anchors += hit["start"] <= a and b <= hit["end"]
                    contexts += any(
                        h["context_start"] <= c and d <= h["context_end"]
                        for h in hits
                        if h["version_id"] == vid
                    )
        out[k] = dict(
            n=n,
            eligible=not unknown and bool(relevant),
            ndcg=dcg([grades[v] for v in top]) / dcg(ideal[: min(k, len(rows))])
            if not unknown and dcg(ideal[:k])
            else None,
            recall=len(exact) / len(relevant) if relevant and not unknown else None,
            mrr=(1 / exact[0] if exact else 0) if relevant and not unknown else None,
            precision=len(exact) / n if n and not unknown else None,
            topic=sum((grades[v] or 0) > 0 for v in top) / n if n and not unknown else None,
            denial_unclear=sum(
                (labels[v] or {}).get("stance") in {"denial", "unclear"} for v in top
            ),
            unknown=unknown,
            missed=sorted(relevant - set(top)),
            anchor_hits=anchors,
            context_hits=contexts,
            span_total=spans,
        )
    return out


def phase(out, split, group):
    from reviewscope.embeddings.candidate_minilm import offline_encoder

    protocol, rows = frozen()
    rows = [r for r in rows if r["split"] == split]
    docs = {r["id"]: Document(r["id"], text_sha(r["text"]), r["text"]) for r in rows}
    queries = [q for q in protocol["queries"] if q["split"] == split]
    start, cpu = monotonic(), process_time()
    info = dict(
        status="RUN",
        split=split,
        group=group,
        methods={},
        runtime={p: version(p) for p in ("numpy", "scikit-learn")},
    )
    if group != "lexical":
        from reviewscope.embeddings.sentence_transformer import installed_runtime

        info["ml_runtime"] = installed_runtime()
    if group.startswith("ce-"):
        name = group[3:]
        parent = json.loads((out / f"{split}-{name}.json").read_text())
        if parent["status"] != "RUN":
            return dict(status="PARENT_NOT_RUN", reason=parent["status"])
        model = CrossEncoder()
        info["load_s"] = monotonic() - start
        key = "rerank_" + name
        ref = parent["methods"]["hybrid_" + name]
        method = info["methods"][key] = dict(queries={})
        for q in queries:
            pools = {}
            for budget, cap in ((10, 40), (50, 200)):
                raw = ref["queries"][q["id"]][str(budget)]
                hits = raw["hits"]
                before = monotonic()
                ranks = reorder(
                    dict(
                        candidates=hits, candidate_count=len(hits), query_sha256=raw["query_sha256"]
                    ),
                    docs,
                    q["query"],
                    model,
                    limit=cap,
                )
                elapsed = monotonic() - before
                by_id = {h["fragment_id"]: h for h in hits}
                ordered = [dict(by_id[r["fragment_id"]], rerank=r) for r in ranks]
                pools[budget] = dict(
                    hits=ordered,
                    seconds=elapsed,
                    pairs=len(hits),
                    pool_sha=digest(sorted(by_id)),
                    query_sha256=raw["query_sha256"],
                    metrics=evaluate(ordered, rows, q["aspect"]),
                )
            method["queries"][q["id"]] = pools
    else:
        encoder = None if group == "lexical" else offline_encoder() if group == "minilm" else E5()
        info["load_s"] = monotonic() - start
        if encoder:
            info["encoder"] = asdict(encoder.contract)
        methods = (
            [("whole_lexical", "lexical", True), ("fragment_lexical", "lexical", False)]
            if not encoder
            else ([("whole_minilm", "dense", True)] if group == "minilm" else [])
            + [("fragment_" + group, "dense", False), ("hybrid_" + group, "hybrid", False)]
        )
        for key, mode, whole in methods:
            before = monotonic()
            cls = E5CandidateIndex if group == "e5" else CandidateIndex
            index = cls(
                dict(dataset="rerank-authored-1", split=split),
                list(docs.values()),
                mode,
                encoder,
                whole=whole,
            )
            method = info["methods"][key] = dict(
                build_s=monotonic() - before,
                diagnostics=index.diagnostics,
                offsets=[
                    [s.version_id, s.start, s.end, s.context_start, s.context_end]
                    for s in index.spans
                ],
                manifest=index.manifest,
                truncated=index.truncated,
                queries={},
            )
            for q in queries:
                pools = {}
                for budget in (10, 50):
                    before = monotonic()
                    result = index.search(q["query"], budget, q["role"])
                    hits = result["candidates"]
                    pools[budget] = dict(
                        hits=hits,
                        seconds=monotonic() - before,
                        query_sha256=result["query_sha256"],
                        pool_sha=digest(sorted(h["fragment_id"] for h in hits)),
                        stages=index.query_timings,
                        metrics=evaluate(hits, rows, q["aspect"]),
                    )
                method["queries"][q["id"]] = pools
    info.update(wall_s=monotonic() - start, cpu_s=process_time() - cpu)
    return info


def rss(pid):
    try:
        return int(
            next(
                s.split()[1]
                for s in Path(f"/proc/{pid}/status").read_text().splitlines()
                if s.startswith("VmRSS:")
            )
        )
    except (StopIteration, FileNotFoundError, ProcessLookupError):
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--split", choices=("dev", "test"))
    args = parser.parse_args()
    if {name for _, name in socket.if_nameindex()} - {"lo"} or any(
        os.getenv(k) != "1" for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    ):
        raise SystemExit("OFFLINE_REQUIRED")
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    if args.phase:
        try:
            report = phase(out, args.split, args.phase)
        except ValueError as exc:
            if str(exc) not in FALLBACK:
                raise
            report = dict(status=str(exc))
        (out / f"{args.split}-{args.phase}.json").write_text(json.dumps(report, allow_nan=False))
        return
    for split in ("dev", "test"):
        for group in PHASES:
            dest = out / f"{split}-{group}.json"
            if dest.exists():
                raise SystemExit("use a fresh output directory")
            start = monotonic()
            peak = combined = 0
            limit = False
            with subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "scripts.benchmark_rerank",
                    str(out),
                    "--phase",
                    group,
                    "--split",
                    split,
                ]
            ) as child:
                while child.poll() is None:
                    current = rss(child.pid)
                    peak = max(peak, current)
                    combined = max(combined, current + rss("self"))
                    if current > 2 * 1024**2 or monotonic() - start >= 120:
                        child.kill()
                        limit = True
                        break
                    sleep(0.05)
            if limit:
                report = dict(status="RESOURCE_LIMIT")
            elif child.returncode:
                raise SystemExit(f"{split}/{group} exited {child.returncode}")
            else:
                report = json.loads(dest.read_text())
            report["process"] = dict(
                wall_s=monotonic() - start,
                peak_sampled_rss_kib=peak,
                parent_plus_child_kib=combined,
            )
            dest.write_text(json.dumps(report, allow_nan=False))
            print(split, group, report["status"], flush=True)


if __name__ == "__main__":
    main()
