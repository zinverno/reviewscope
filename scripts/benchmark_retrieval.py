#!/usr/bin/env python3
"""Offline retrieval experiment. Usage: python scripts/benchmark_retrieval.py --help"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from tempfile import mkdtemp
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402
from sklearn.preprocessing import normalize  # noqa: E402
from sklearn.utils.extmath import safe_sparse_dot  # noqa: E402

from reviewscope.embeddings import SentenceTransformerProvider  # noqa: E402
from reviewscope.embeddings.base import embedding_key  # noqa: E402
from reviewscope.validation.retrieval_dataset import Dataset, digest, load_dataset  # noqa: E402
from reviewscope.validation.retrieval_metrics import evaluate_query, summarize  # noqa: E402

FIXTURE = ROOT / "tests/data/fixtures/retrieval_smoke.json"
OUTPUT = ROOT / "validation_data/benchmarks"
SEED = 42  # Fixed tie order for every method, split and run.


def timed(fn, *args):
    start = perf_counter()
    result = fn(*args)
    return result, perf_counter() - start


def local_minilm():
    provider = SentenceTransformerProvider()
    try:
        from huggingface_hub import constants, try_to_load_from_cache
    except ImportError:
        return None, "NOT_RUN_MISSING_BACKEND"
    if not constants.HF_HUB_OFFLINE:
        raise RuntimeError("run the benchmark CLI in a fresh offline process")
    weights = try_to_load_from_cache(
        provider.name, "model.safetensors", revision=provider.contract.revision
    )
    if not isinstance(weights, str) or not Path(weights).is_file():
        return None, "NOT_RUN_MISSING_WEIGHTS"
    from reviewscope.embeddings.sentence_transformer import installed_runtime

    if installed_runtime() is None:
        return None, "NOT_RUN_MISSING_BACKEND"
    provider._load()  # Time the same revision, dimension and length checks as PR-04.
    return provider, "RUN"


def benchmark(data: Dataset, split: str, method: str) -> dict:
    docs = sorted((d for d in data.documents if d.split == split), key=lambda d: d.review_id)
    if not docs:
        raise ValueError("no documents in selected split")
    ids = [d.review_id for d in docs]
    texts = [d.text for d in docs]
    queries = sorted(data.queries, key=lambda q: q.query_id)
    times = {}
    if method == "minilm":
        (provider, status), times["model_load_s"] = timed(local_minilm)
        if provider is None:
            return {"status": status}
        contract = asdict(provider.contract)
        encoding_id = provider.contract.encoding_id
        encode = provider.encode
    else:
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), dtype=np.float32)
        _, times["vocabulary_idf_fit_s"] = timed(vectorizer.fit, texts)
        contract = {
            "implementation": "sklearn.TfidfVectorizer",
            "sklearn": version("scikit-learn"),
            "parameters": {**vectorizer.get_params(), "dtype": "float32"},
            "fit_documents_sha256": digest(list(zip(ids, texts, strict=True))),
        }
        encoding_id = "tfidf-v1:" + digest(contract)
        encode = vectorizer.transform

    session = {}
    role_ids = {}

    def batch(role, row_ids, inputs):
        role_id = role_ids.setdefault(role, digest([encoding_id, role, "verbatim-no-prefix-v1"]))
        keys = tuple(embedding_key(i, t, role_id) for i, t in zip(row_ids, inputs, strict=True))
        if keys not in session:
            session[keys] = encode(inputs)
        return session[keys]

    documents, times["cold_document_encode_s"] = timed(batch, "document", ids, texts)
    _, times["warm_document_cache_s"] = timed(batch, "document", ids, texts)
    index, times["index_build_s"] = timed(lambda: normalize(documents).T)
    tie_keys = [digest([SEED, rid]) for rid in ids]
    labels = defaultdict(dict)
    for j in data.judgments:
        labels[j.query_id][j.review_id] = j
    cutoffs = [k for k in (5, 10, 20, 50) if k <= len(ids)] or [len(ids)]
    results = {}
    mode_times = {}
    for mode in sorted({q.mode for q in queries}):
        subset = [q for q in queries if q.mode == mode]
        qids = [q.query_id for q in subset]
        qtexts = [q.query_text for q in subset]
        matrix, cold_s = timed(batch, mode, qids, qtexts)
        _, warm_s = timed(batch, mode, qids, qtexts)
        _, repeat_s = timed(encode, qtexts)
        search_s = 0.0
        for query, vec in zip(subset, normalize(matrix), strict=True):
            start = perf_counter()
            scores = np.asarray(
                safe_sparse_dot(vec.reshape(1, -1), index, dense_output=True)
            ).ravel()
            positions = sorted(range(len(ids)), key=lambda i: (-scores[i], tie_keys[i], ids[i]))
            ranked = [ids[i] for i in positions]
            search_s += perf_counter() - start
            row = evaluate_query(ranked, labels[query.query_id], cutoffs)
            row.update(
                mode=mode, problem_id=query.problem_id, definition_version=query.definition_version
            )
            row["top"] = [
                {
                    "review_id": ids[i],
                    "cosine": float(scores[i]),
                    "relevance": j.relevance if (j := labels[query.query_id].get(ids[i])) else None,
                    "stance": j.stance if j else None,
                }
                for i in positions[: max(cutoffs)]
            ]
            results[query.query_id] = row
        mode_times[mode] = {
            "first_query_encode_s": cold_s,
            "warm_query_cache_s": warm_s,
            "warm_uncached_query_encode_s": repeat_s,
            "search_sort_s": search_s,
        }
    return {
        "status": "RUN",
        "contract": contract,
        "encoding_id": encoding_id,
        "role_ids": role_ids,
        "dimension": documents.shape[1],
        "cutoffs": cutoffs,
        "summary_by_mode": summarize(queries, results),
        "queries": results,
        "timings": {**times, "by_mode": mode_times},
    }


def resources() -> dict:
    cpu = platform.processor() or platform.machine()
    peak_kib = None
    if sys.platform.startswith("linux"):
        import resource

        peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            cpu = next(
                (
                    line.partition(":")[2].strip()
                    for line in cpuinfo.read_text().splitlines()
                    if line.startswith("model name")
                ),
                cpu,
            )
    return {
        "cpu": cpu,
        "cpu_count_visible": len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": version("scipy"),
        "sklearn": version("scikit-learn"),
        "process_peak_rss_kib": peak_kib,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=FIXTURE)
    parser.add_argument("--split", choices=["dev", "test", "future", "novel_place"], default="dev")
    parser.add_argument(
        "--minilm", action="store_true", help="also run the cached PR-04 revision, offline only"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--fingerprint", action="store_true", help="compute the digest for a prepared manifest"
    )
    actions.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = load_dataset(args.dataset, check_fingerprint=not args.fingerprint)
        path = args.dataset.resolve()
        if (
            data.manifest.kind == "private"
            and path.is_relative_to(ROOT)
            and not path.is_relative_to(ROOT / "validation_data/private")
        ):
            raise ValueError(
                "keep private input outside the checkout or under validation_data/private"
            )
        if args.fingerprint or args.validate_only:
            print(data.fingerprint() if args.fingerprint else "VALID")
            return 0
        methods = ["tfidf", "minilm"] if args.minilm else ["tfidf"]
        if not set(methods) <= set(data.manifest.methods):
            raise ValueError("requested method outside frozen plan")
        if (
            data.manifest.kind == "private"
            and args.split != "dev"
            and set(methods) != set(data.manifest.methods)
        ):
            raise ValueError("held-out run requires the complete predeclared method plan")
        docs = [d for d in data.documents if d.split == args.split]
        from reviewscope.validation import retrieval_dataset, retrieval_metrics

        sources = [
            Path(__file__),
            Path(retrieval_dataset.__file__),
            Path(retrieval_metrics.__file__),
        ]
        report = {
            "protocol": "reviewscope-retrieval-v1",
            "dataset_id": data.manifest.dataset_id,
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
            "dataset_fingerprint": data.manifest.fingerprint,
            "kind": data.manifest.kind,
            "annotation_version": data.manifest.annotation_version,
            "planned_methods": data.manifest.methods,
            "split": args.split,
            "tie_seed": SEED,
            "documents": len(docs),
            "queries": len(data.queries),
            "language_counts": dict(Counter(d.language or "unknown" for d in docs)),
            "methods": {m: benchmark(data, args.split, m) for m in methods},
            "resources": resources(),
        }
        OUTPUT.mkdir(parents=True, exist_ok=True)
        path = Path(mkdtemp(prefix="run-", dir=OUTPUT)) / "report.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        print(path.relative_to(ROOT))
    except ValidationError as exc:
        for err in exc.errors(include_input=False, include_context=False, include_url=False):
            print(f"{'.'.join(map(str, err['loc']))}: {err['msg']}", file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        DO_NOT_TRACK="1",
        TOKENIZERS_PARALLELISM="false",
    )
    raise SystemExit(main())
