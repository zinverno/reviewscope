"""Summarize the frozen holdout and audit overlap without changing predictions or qrels."""

import gzip
import json
from collections import Counter
from itertools import combinations
from pathlib import Path

from rapidfuzz.fuzz import ratio

from scripts.freeze_relation_corpus import ASPECTS, ROOT

OUT = Path("docs/development/typed-relation-evidence")
FIX = Path("tests/data/fixtures")
POS = ("same_specific_complaint", "opposite_stance")


def read_lines(path):
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return [json.loads(line) for line in raw.splitlines()]


def overlap():
    new = read_lines(ROOT / "reviews.jsonl.gz")
    earlier = {
        "05": json.loads((FIX / "retrieval_smoke.json").read_bytes())["documents"],
        "19": sum(
            json.loads((FIX / "candidate_challenge_v1.json").read_bytes())["splits"].values(), []
        ),
        "21": read_lines(FIX / "rerank_reviews_v1.jsonl"),
        "22": read_lines(FIX / "decision_reviews_v1.jsonl"),
    }

    def measure(pairs):
        exact = close = total = 0
        highest = 0
        for a, b in pairs:
            v = ratio(" ".join(a["text"].lower().split()), " ".join(b["text"].lower().split()))
            exact += int(v == 100)
            close += int(v >= 85)
            total += 1
            highest = max(highest, v)
        return dict(
            compared=total, normalized_equal=exact, rapidfuzz_ge_85=close, max_ratio=highest / 100
        )

    return {k: measure((a, b) for a in new for b in old) for k, old in earlier.items()} | {
        "between_splits": measure(
            (a, b) for a, b in combinations(new, 2) if a["split"] != b["split"]
        )
    }


def slice_report(block, axis, label, method):
    with_labels = [r for r in block if r["gold"][axis] is not None]
    positives = [r for r in with_labels if r["gold"][axis] == label]
    return dict(
        known=len(with_labels),
        positive=len(positives),
        selected_positive=sum(bool(r["channels"]) for r in positives),
        tp=sum(bool(r["channels"]) and r["predict"][method] == label for r in positives),
        fp=sum(
            bool(r["channels"]) and r["predict"][method] == label and r["gold"][axis] != label
            for r in with_labels
        ),
    )


def challenge(r, kind, docs, axis):
    endpoints = [docs[i] for i in r["id"].split("/")]
    if kind == "long_tail":
        return any(len(e["text"]) > 500 for e in endpoints)
    if kind == "identical_or_normalized":
        return r["gold"]["text"] == "exact_text"
    if kind == "wording_change":
        return r["gold"]["text"] == "near_copy"
    return axis != "text" and any(e["stances"][axis] == kind for e in endpoints)


def main():
    rows = read_lines(OUT / "test-pairs.jsonl.gz")
    docs = {r["id"]: r for r in read_lines(ROOT / "reviews.jsonl.gz")}
    selection = json.loads((OUT / "selection.json").read_bytes())["selected"]
    summary = {"overlap": overlap(), "quality": {}}
    for axis in ("text", *ASPECTS):
        method = selection.get(axis, "text")
        labels = ("exact_text", "near_copy") if axis == "text" else POS
        critical = Counter()
        for r in rows:
            if not r["channels"] or axis == "text":
                continue
            gold, pred = r["gold"][axis], r["predict"][method]
            if pred in POS and gold in POS and pred != gold:
                critical["opposite_shared_swap"] += 1
            if gold == "no_relation" and pred == "same_specific_complaint":
                critical["other_to_shared"] += 1
        stats = {}
        for label in labels:
            stats[label] = {
                k: slice_report(
                    [r for r in rows if challenge(r, k, docs, axis)], axis, label, method
                )
                for k in ("long_tail", "identical_or_normalized", "wording_change", "M", "D", "U")
            }
            stats[label]["all"] = slice_report(rows, axis, label, method)
        summary["quality"][axis] = dict(method=method, critical=critical, slices=stats)
    (OUT / "audit.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
