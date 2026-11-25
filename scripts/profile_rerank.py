"""Bounded cold rebuild and warm 40-pair CPU measurement on authored text only."""

import hashlib
import json
import os
import socket
import subprocess
import sys
from statistics import median
from time import monotonic, process_time, sleep

from scripts.benchmark_rerank import FIX, rss


def reviews():
    rows = [
        json.loads(s)["text"] for s in (FIX / "rerank_reviews_v1.jsonl").read_text().splitlines()
    ]
    for i in range(10000):
        yield f"За столиком {i + 1} записали впечатление. {rows[i % len(rows)]} Заходили в {8 + i % 14} ч, на полке лежал талон {i + 1}."


def measure(stage):
    from operational.worker import linux_peak_kib
    from reviewscope.candidates import CandidateIndex, Document, anchors, text_sha
    from reviewscope.embeddings.candidate_minilm import offline_encoder
    from reviewscope.embeddings.rerank import CrossEncoder

    start, cpu = monotonic(), process_time()
    if stage == "pairs":
        model = CrossEncoder()
        loaded = monotonic()
        windows = [(str(i), text, text) for i, text in zip(range(40), reviews(), strict=False)]
        times = []
        for _ in range(5):
            tick = monotonic()
            scores = model.score("Когда пришлось ждать?", windows)
            times.append(monotonic() - tick)
            if len(scores) != 40:
                raise ValueError("INVALID_POOL")
        output = dict(
            pairs=40,
            unique_contexts=40,
            load_s=loaded - start,
            n=len(times),
            p50_s=median(times),
            max_s=max(times),
            tokens=sum(model.length("Когда пришлось ждать?", w[2]) for w in windows),
        )
    else:
        docs = [Document(str(i), text_sha(text), text) for i, text in enumerate(reviews())]
        distinct = {d.text[a:b] for d in docs for a, b in anchors(d.text)}
        info = dict(
            versions=len(docs),
            bytes=sum(len(d.text.encode()) for d in docs),
            unique_character_anchors=len(distinct),
            text_sha256=hashlib.sha256("\n".join(d.text for d in docs).encode()).hexdigest(),
        )
        print(json.dumps(dict(input=info)), flush=True)
        encoder = offline_encoder()
        loaded = monotonic()
        # Tokenize distinct anchors once to record the workload even if CPU admission stops encoding.
        print(
            json.dumps(
                dict(
                    unique_inputs=len(distinct),
                    input_tokens=sum(encoder.token_count(t) for t in distinct),
                )
            ),
            flush=True,
        )
        index = CandidateIndex({"synthetic": "diverse-v1"}, docs, "hybrid", encoder)
        built = monotonic()
        times = []
        for _ in range(5):
            tick = monotonic()
            index.search("Когда пришлось ждать?", 10)
            times.append(monotonic() - tick)
        output = dict(
            input=info,
            diagnostics=index.diagnostics,
            load_s=loaded - start,
            cold_build_s=built - loaded,
            warm_search_s=times,
        )
    print(
        json.dumps(
            dict(
                status="RUN",
                wall_s=monotonic() - start,
                cpu_s=process_time() - cpu,
                peak_rss_kib=linux_peak_kib("self"),
                **output,
            )
        ),
        flush=True,
    )


def main():
    if {name for _, name in socket.if_nameindex()} - {"lo"} or any(
        os.getenv(k) != "1" for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    ):
        raise SystemExit("OFFLINE_REQUIRED")
    if len(sys.argv) == 2:
        if sys.argv[1] not in ("diverse", "pairs"):
            raise SystemExit("unknown stage")
        measure(sys.argv[1])
        return
    for stage in ("diverse", "pairs"):
        start = monotonic()
        peak = combined = 0
        limit = None
        with subprocess.Popen(
            [sys.executable, "-m", "scripts.profile_rerank", stage],
            stdout=subprocess.PIPE,
            text=True,
        ) as child:
            while child.poll() is None:
                current = rss(child.pid)
                peak = max(peak, current)
                combined = max(combined, current + rss("self"))
                if current > 2 * 1024**2 or monotonic() - start >= 120:
                    limit = "child_rss" if current > 2 * 1024**2 else "wall"
                    child.kill()
                    break
                sleep(0.05)
            raw, _ = child.communicate()
        measurements = [json.loads(s) for s in raw.splitlines()]
        if child.returncode and not limit:
            raise SystemExit(child.returncode)
        print(
            json.dumps(
                dict(
                    stage=stage,
                    status="RESOURCE_LIMIT" if limit else "RUN",
                    limit=limit,
                    wall_s=monotonic() - start,
                    sampled_peak_kib=peak,
                    parent_plus_child_kib=combined,
                    measurements=measurements,
                )
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
