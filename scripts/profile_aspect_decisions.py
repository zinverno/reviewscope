"""Bound the calibration-selected research model on three public synthetic workloads."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic, process_time, sleep


def workload(dataset):
    if dataset == "diverse":
        from scripts.profile_rerank import reviews

        return list(reviews())
    if dataset == "repeated":
        from reviewscope.import_preview.contract import MAX_BYTES, MAX_ROWS

        width, extra = divmod(MAX_BYTES - len(b"id,body\n") - MAX_ROWS * len(b"00000,\n"), MAX_ROWS)
        return ["t" * (width + (i < extra)) for i in range(MAX_ROWS)]
    if dataset != "public-demo":
        raise ValueError("INVALID_FIXTURE")
    import duckdb

    from scripts.benchmark_aspect_decisions import ROOT, digest

    path = ROOT / "demo/reviewscope_demo.duckdb"
    # Keep this research reader independent of the optional PostgreSQL operator stack.
    if digest(path) != "85ca0756ca60f56c735360ab46632d8893287635e7fec1a28548f97dae5fcfe2":
        raise ValueError("INVALID_PUBLIC_FIXTURE")
    with duckdb.connect(str(path), read_only=True) as con:
        return [r[0] for r in con.execute("SELECT text FROM reviews ORDER BY review_id").fetchall()]


def measure(checkpoint, dataset):
    import numpy as np

    from scripts.benchmark_aspect_decisions import ROOT, Features, aggregate, digest

    evidence = ROOT / "docs/development/aspect-decision-evidence"
    selection = json.loads((evidence / "selection.json").read_bytes())
    model = selection["selected"]
    asset = evidence / f"{model}.npz"
    if digest(asset) != selection["methods"][model]["sha256"]:
        raise ValueError("INVALID_HEAD")
    accepted = workload(dataset)
    texts = [text for text in accepted if text and text.strip()]
    state = dict(
        model=model,
        dataset=dataset,
        accepted=len(accepted),
        excluded_blank=len(accepted) - len(texts),
        loadavg_start=os.getloadavg(),
        aspect="queue",
        eligible=len(texts),
        unique_inputs=len(set(texts)),
        corpus_sha256=hashlib.sha256("\n".join(texts).encode()).hexdigest(),
        evaluated=0,
        windows=0,
        encoded_tokens=0,
        published=False,
    )

    def save():
        tmp = checkpoint.with_suffix(".new")
        tmp.write_text(json.dumps(state))
        tmp.replace(checkpoint)

    save()
    start, cpu = monotonic(), process_time()
    features = Features(model)
    with np.load(asset, allow_pickle=False) as weights:
        coef, bias = weights["coef_queue"], weights["bias_queue"]
        threshold = float(weights["threshold"])
    state["prepare_s"] = monotonic() - start
    head_s = agg_s = 0.0
    outcomes = hashlib.sha256()
    for offset in range(0, len(texts), 32):
        batch = [{"text": t} for t in texts[offset : offset + 32]]
        parts, x = features.compute(batch, "queue")
        tick = monotonic()
        scores = x @ coef.T + bias
        head_s += monotonic() - tick
        tick = monotonic()
        n = 0
        for spans in parts:
            result = aggregate(spans, scores[n : n + len(spans)], threshold)
            outcomes.update(
                json.dumps(result, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            )
            n += len(spans)
        assert n == len(scores)
        agg_s += monotonic() - tick
        state.update(
            evaluated=offset + len(batch),
            windows=features.window_count,
            encoded_tokens=features.tokens,
            unique_encoded=len(features.cache),
            encode_s=features.encode_s,
            segment_s=features.segment_s,
            head_s=head_s,
            aggregate_s=agg_s,
            cpu_s=process_time() - cpu,
        )
        if state["evaluated"] == state["eligible"]:
            state["outputs_sha256"] = outcomes.hexdigest()
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", choices=("diverse", "repeated", "public-demo"), default="diverse"
    )
    parser.add_argument("--checkpoint", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.checkpoint:
        measure(args.checkpoint, args.dataset)
        return
    from scripts.benchmark_rerank import rss

    with tempfile.TemporaryDirectory() as tmp:
        checkpoint = Path(tmp) / "counts.json"
        start = monotonic()
        peak = combined = 0
        limit = None
        with subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.profile_aspect_decisions",
                "--dataset",
                args.dataset,
                "--checkpoint",
                str(checkpoint),
            ]
        ) as child:
            while child.poll() is None:
                current = rss(child.pid)
                peak = max(peak, current)
                combined = max(combined, current + rss("self"))
                if current > 2 * 1024**2 or monotonic() - start >= 120:
                    limit = "rss" if current > 2 * 1024**2 else "wall"
                    child.kill()
                    break
                sleep(0.05)
            child.wait()
        if not limit and child.returncode:
            raise SystemExit(child.returncode)
        state = json.loads(checkpoint.read_bytes())
        if not limit and state["evaluated"] != state["eligible"]:
            raise ValueError("INCOMPLETE")
        print(
            json.dumps(
                dict(
                    **state,
                    status="RESOURCE_LIMIT" if limit else "COMPLETE",
                    limit=limit,
                    exit_code=child.returncode,
                    wall_s=monotonic() - start,
                    sampled_child_peak_kib=peak,
                    simultaneous_parent_child_peak_kib=combined,
                )
            )
        )


if __name__ == "__main__":
    main()
