"""Measure portable text selection on synthetic snapshot projections supplied by the native test."""

import hashlib
import json
import resource
import sys
from time import monotonic, process_time

from operational.worker import linux_peak_kib
from reviewscope.typed_relations import lexical_candidates


def main():
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    rows = json.loads(sys.stdin.buffer.read(8 * 1024**2 + 1))
    docs = {k: v or "" for k, v in rows}
    if len(docs) != len(rows):
        raise ValueError("DUPLICATE_VERSION")
    runs = []
    for _ in range(2):
        start, cpu = monotonic(), process_time()
        pairs, summary = lexical_candidates(docs)
        digest = hashlib.sha256()
        size = 0
        for (a, b), scores in sorted(pairs.items()):
            raw = json.dumps([a, b, scores], sort_keys=True, separators=(",", ":")).encode() + b"\n"
            digest.update(raw)
            size += len(raw)
        runs.append(
            dict(
                **summary,
                wall_s=monotonic() - start,
                cpu_s=process_time() - cpu,
                output_bytes=size,
                pair_sha256=digest.hexdigest() if summary["status"] == "complete" else None,
                peak_child_rss_kib=linux_peak_kib("self"),
            )
        )
    assert runs[0]["pair_sha256"] == runs[1]["pair_sha256"]
    print(json.dumps(runs))


if __name__ == "__main__":
    main()
