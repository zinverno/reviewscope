"""Recall lost by restricting decisions to PR-19/21 search on the frozen PR-22 holdout."""

import argparse
import gzip
import json
from pathlib import Path

from reviewscope.aspect_decisions import COMPLAINTS, definitions
from reviewscope.candidates import CandidateIndex, Document, text_sha
from reviewscope.embeddings.candidate_minilm import offline_encoder
from reviewscope.embeddings.rerank import CrossEncoder
from reviewscope.rerank import reorder
from scripts.benchmark_aspect_decisions import Features, load_rows, write_json


def run(out, stage):
    rows = load_rows({"test"})
    docs = {r["id"]: Document(r["id"], text_sha(r["text"]), r["text"]) for r in rows}
    if stage == "retrieve":
        verified = Features("minilm")
        del verified
        index = CandidateIndex(
            {"dataset": "aspect-decision-1", "split": "test"},
            list(docs.values()),
            "hybrid",
            offline_encoder(),
        )
    else:
        model = CrossEncoder()
        parents = json.loads((out / "control-pools.json").read_text())
    evidence = {}
    selected = json.loads((out / "selection.json").read_text())["selected"]
    with gzip.open(out / f"{selected}-test-rows.jsonl.gz", "rt") as stream:
        predicted = [json.loads(s) for s in stream]
    for key, payload in definitions().items():
        evidence[key] = {}
        query = payload["definition"]
        positive = {r["id"] for r in rows if r["labels"][key]["outcome"] in COMPLAINTS}
        true_positive = {
            r["id"]
            for r in predicted
            if r["aspect"] == key and r["outcome"] in COMPLAINTS and r["gold"] in COMPLAINTS
        }
        for k in (10, 50):
            if stage == "retrieve":
                result = index.search(query, k)
                hits = result["candidates"]
                order = list(dict.fromkeys(h["version_id"] for h in hits))
            else:
                hits = parents[key][str(k)]["hits"]
                parent = dict(
                    candidates=hits, candidate_count=len(hits), query_sha256=text_sha(query)
                )
                ranked = reorder(parent, docs, query, model, limit=4 * k)
                by_id = {h["fragment_id"]: h["version_id"] for h in hits}
                order = list(dict.fromkeys(by_id[h["fragment_id"]] for h in ranked))
            evidence[key][k] = dict(
                ids=order,
                n=len(order),
                eligible=len(rows),
                matched=len(positive & set(order)),
                selected_model_tp_with_retrieval=len(true_positive & set(order)),
                selected_model_tp_all=len(true_positive),
                positives=len(positive),
                missing=sorted(positive - set(order)),
            )
            if stage == "retrieve":
                evidence[key][k]["hits"] = hits
    write_json(
        out / ("control-pools.json" if stage == "retrieve" else "control-rerank.json"), evidence
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    p.add_argument("stage", choices=("retrieve", "rerank"))
    args = p.parse_args()
    run(args.output, args.stage)
