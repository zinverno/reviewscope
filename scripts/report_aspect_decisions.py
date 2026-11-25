"""Publish anonymous synthetic benchmark evidence from two independently run offline inferences."""

import argparse
import gzip
import json
import platform
import shutil
from collections import Counter
from pathlib import Path
from statistics import mean

import numpy as np

from reviewscope.aspect_decisions import (
    COMPLAINTS,
    LABELS,
    PROFILE,
    DecisionScorer,
    aggregate,
    definitions,
)
from scripts.benchmark_aspect_decisions import METHODS, digest, load_rows, metrics, write_json


def read(path):
    return json.loads(path.read_bytes())


def report(first, repeat, evidence):
    evidence.mkdir(exist_ok=True)
    selection = read(first / "selection.json")
    if read(evidence / "selection.json") != selection:
        raise ValueError("SELECTION_CHANGED")
    inputs = load_rows({"train", "cal", "test"})
    by_id = {r["id"]: r for r in inputs}
    summary = dict(
        freeze_commit="c03a640",
        selection_commit="044637c",
        selection=selection["selected"],
        labels=list(LABELS),
        source="authored_synthetic",
        independent_real_labels=False,
        denominator_notes="Every test review x six aspects. Undefined ratios are null because denominator is zero. Binary metrics exclude ambiguous gold polarity. Known-positive abstentions count as FN end-to-end; known-negative abstentions are reported separately. Macro averages include defined per-class values only.",
        split_support={
            s: dict(
                Counter(
                    v["outcome"] for r in inputs if r["split"] == s for v in r["labels"].values()
                )
            )
            for s in ("train", "cal", "test")
        },
        cpu_model=next(
            (
                s.split(":", 1)[1].strip()
                for s in Path("/proc/cpuinfo").read_text().splitlines()
                if s.startswith("model name")
            ),
            None,
        ),
        platform=platform.platform(),
        methods={},
    )
    for method in METHODS:
        calibration_file = f"{method}-fit-rows.jsonl.gz"
        calibrated = [
            json.loads(s)
            for s in gzip.decompress((first / calibration_file).read_bytes()).splitlines()
        ]
        expected_cal = {(r["id"], a) for r in inputs if r["split"] == "cal" for a in definitions()}
        if {(r["id"], r["aspect"]) for r in calibrated} != expected_cal or len(calibrated) != len(
            expected_cal
        ):
            raise ValueError("WRONG_CALIBRATION_MEMBERSHIP")
        for gap, expected_grid in selection["methods"][method]["grid"].items():
            replayed = []
            for r in calibrated:
                if by_id[r["id"]]["labels"][r["aspect"]]["outcome"] != r["gold"]:
                    raise ValueError("WRONG_CALIBRATION_GOLD")
                replayed.append(r | aggregate(r["windows"], np.asarray(r["logits"]), float(gap)))
            actual = metrics(replayed)
            if [actual["complaint"]["f1"] or 0.0, actual["coverage"]] != expected_grid:
                raise ValueError("CALIBRATION_GRID_CHANGED")
        # The pre-test file predates the two optional, empty counter-context fields.
        for r in calibrated:
            for field in ("counter_start", "counter_end"):
                if r.pop(field, None) is not None:
                    raise ValueError("UNEXPECTED_CALIBRATION_COUNTER_CONTEXT")
        calibration_lines = "".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
            for r in calibrated
        )
        (evidence / calibration_file).write_bytes(
            gzip.compress(calibration_lines.encode(), mtime=0)
        )
        file = f"{method}-test-rows.jsonl.gz"
        if digest(first / file) != digest(repeat / file):
            raise ValueError("REPEAT_MISMATCH")
        shutil.copy(first / file, evidence / file)
        rows = [json.loads(s) for s in gzip.decompress((first / file).read_bytes()).splitlines()]
        expected = {(r["id"], a) for r in inputs if r["split"] == "test" for a in definitions()}
        if len(rows) != len(expected) or {(r["id"], r["aspect"]) for r in rows} != expected:
            raise ValueError("WRONG_MEMBERSHIP")
        control = {}
        if method == "lexical":
            control = {
                a: DecisionScorer({"origin": "manual", "payload": d}, PROFILE)
                for a, d in definitions().items()
            }
        mistakes = []
        for r in rows:
            review = by_id[r["id"]]
            gold = review["labels"][r["aspect"]]
            if gold["outcome"] != r["gold"]:
                raise ValueError("WRONG_GOLD")
            if control and control[r["aspect"]].score(review["text"]) != {
                k: r[k]
                for k in (
                    "outcome",
                    "start",
                    "end",
                    "counter_start",
                    "counter_end",
                    "score",
                    "reason",
                )
            }:
                raise ValueError("PORTABLE_MODEL_DRIFT")
            if (
                r["gold"] != r["outcome"]
                and r["gold"] != "unknown"
                and (r["gold"] in COMPLAINTS or r["outcome"] in COMPLAINTS)
            ):
                mistakes.append(
                    dict(
                        id=r["id"],
                        aspect=r["aspect"],
                        gold=r["gold"],
                        outcome=r["outcome"],
                        model_context=[r["start"], r["end"]],
                        gold_spans=gold["spans"],
                    )
                )
        m = metrics(rows)
        known_positive = sum(
            by_id[i]["labels"][a]["outcome"] in ("complaint", "mixed") for i, a in expected
        )
        observed = [r for r in rows if r["gold"] in COMPLAINTS]
        if (
            known_positive != len(observed)
            or sum(r["outcome"] in COMPLAINTS for r in observed) != m["complaint"]["tp"]
        ):
            raise ValueError("COMPLAINT_ORACLE_DISAGREES")

        def passes(b):
            return (
                (b["precision"] or 0) >= 0.85 and (b["recall"] or 0) >= 0.8 and b["coverage"] >= 0.8
            )

        m["gate_pass"] = (
            passes(m["complaint"] | {"coverage": m["coverage"]})
            and all(
                passes(s) and s["tp"] + s["fn"] >= 3
                for s in (m["per_aspect"]["queue"], m["per_aspect"]["handover"])
            )
            and m["denial_fp"][0] == 0
            and m["denial_fp"][1] > 0
            and all(d and n / d >= 0.80 for n, d in (m["mixed_recall"], m["tail_recall"]))
        )
        m["macro_aspect_complaint"] = {
            k: mean(v[k] for v in m["per_aspect"].values() if v[k] is not None)
            for k in ("precision", "recall", "f1")
        }
        summary["methods"][method] = dict(
            metrics=m,
            errors=mistakes,
            exact_repeat_sha256=digest(first / file),
            fit_resources=read(first / f"{method}-fit-resource.json"),
            test_resources=[
                read(folder / f"{method}-test-resource.json") for folder in (first, repeat)
            ],
        )
    summary["retrieval"] = {}
    for stage, file in (("PR19", "control-pools.json"), ("PR21", "control-rerank.json")):
        control = read(first / file)
        for a in control.values():
            for pool in a.values():
                pool.pop("hits", None)
        summary["retrieval"][stage] = control
    write_json(evidence / "summary.json", summary)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("first", "repeat", "evidence"):
        p.add_argument(name, type=Path)
    a = p.parse_args()
    report(a.first, a.repeat, a.evidence)
