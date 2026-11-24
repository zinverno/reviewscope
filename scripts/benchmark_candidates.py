"""Frozen offline PR-19 experiment. Stdout contains authored synthetic results only."""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmark_retrieval import FIXTURE, resources  # noqa: E402

from reviewscope.candidates import CandidateIndex, Document, text_sha  # noqa: E402
from reviewscope.embeddings.candidate_minilm import offline_encoder  # noqa: E402
from reviewscope.validation.retrieval_dataset import load_dataset  # noqa: E402
from reviewscope.validation.retrieval_metrics import dcg  # noqa: E402

CHALLENGE = FIXTURE.with_name("candidate_challenge_v1.json")
SHA = "d94da12cd890623ade493ca7ba804f8d38bd46af41ad258ea36a2b93a034d5d5"
KS = (1, 3, 5, 10, 20, 50)


def evaluate(corpus_ids, candidates, labels, k):
    if len(set(corpus_ids)) != len(corpus_ids) or not set(labels) <= set(corpus_ids):
        raise ValueError("invalid evaluation corpus")
    first = {}
    for row in candidates:
        if row["version_id"] not in corpus_ids:
            raise ValueError("unknown version")
        first.setdefault(row["version_id"], row)
    ranked = list(first)[:k]
    unknown = sum(labels.get(i, (None,))[0] is None for i in corpus_ids)
    relevant = {i for i, (g, _, _) in labels.items() if g == 2}
    hits = [r for r, i in enumerate(ranked, 1) if i in relevant]
    spans = [(i, span) for i in relevant for span in labels[i][2]]
    covered = sum(
        i in ranked
        and first[i]["start"] <= a < b <= first[i]["end"]
        and first[i]["context_start"] <= c < d <= first[i]["context_end"]
        for i, (a, b, c, d) in spans
    )

    def grade(i):
        return labels.get(i, (None,))[0]

    eligible = bool(relevant and not unknown)
    return dict(
        effective_k=min(k, len(corpus_ids)),
        returned=len(ranked),
        known_hits=len(hits),
        relevant=len(relevant),
        unknown=unknown,
        unjudged_in_top=sum(grade(i) is None for i in ranked),
        recall=len(hits) / len(relevant) if eligible else None,
        mrr=(1 / hits[0] if hits else 0) if eligible else None,
        ndcg=dcg([grade(i) for i in ranked])
        / dcg(sorted((grade(i) for i in corpus_ids), reverse=True)[:k])
        if eligible
        else None,
        span_hits=covered,
        span_total=len(spans),
        false_positive=dict(Counter(labels[i][1] for i in ranked if grade(i) in (0, 1))),
        missed={i: labels[i][1] for i in sorted(relevant - set(ranked))},
    )


def inputs():
    old = load_dataset(FIXTURE)
    docs = {d.review_id: d.text for d in old.documents}
    labels = {}
    for j in old.judgments:
        spans = []
        if j.evidence_span:
            a = docs[j.review_id].index(j.evidence_span)
            b = a + len(j.evidence_span)
            spans.append((a, b, a, b))
        labels.setdefault(j.query_id, {})[j.review_id] = (j.relevance, j.stance, spans)
    yield (
        "pr05-dev",
        old.fingerprint(),
        docs,
        [(q.query_id, q.mode, q.query_text, labels[q.query_id]) for q in old.queries],
    )
    if hashlib.sha256(CHALLENGE.read_bytes()).hexdigest() != SHA:
        raise ValueError("challenge fingerprint mismatch")
    data = json.loads(CHALLENGE.read_text())
    seen = set()
    for split, rows in data["splits"].items():
        families = {r["family"] for r in rows}
        texts = {" ".join(r["text"].casefold().split()) for r in rows}
        if seen & (families | texts):
            raise ValueError("split leakage")
        seen.update(families | texts)
        docs = {r["id"]: r["text"] for r in rows}
        if len(docs) != len(rows):
            raise ValueError("duplicate document")
        qrels = {r["id"]: (r["grade"], r["stance"], r["spans"]) for r in rows}
        yield split, SHA, docs, [(*q, qrels) for q in data["queries"]]


def public_profile(encoder):
    from reviewscope.storage import DuckDBStore
    from scripts.build_cabinet_demo import DEMO, DEMO_SHA, OUTPUT, public_review, result_id

    if hashlib.sha256(DEMO.read_bytes()).hexdigest() != DEMO_SHA:
        raise ValueError("unverified_public_demo")
    places = {p["id"]: p["source_result"] for p in json.loads(OUTPUT.read_bytes())["places"]}
    with DuckDBStore(DEMO, read_only=True) as store:
        store.connection().execute("SET enable_external_access=false")
        rows = store.fetch_reviews()
    docs = []
    for place, source in places.items():
        frozen = json.loads((OUTPUT.parent / "overview-sources" / (place + ".json")).read_bytes())
        if frozen.pop("result") != source or result_id(frozen) != source:
            raise ValueError("unverified_public_source")
        for row in (r for r in rows if r.place_id == place):
            version = public_review(row)["version"]
            if frozen["reviews"][row.review_id]["version"] != version:
                raise ValueError("unverified_public_version")
            docs.append(
                Document(
                    json.dumps([place, source, row.review_id, version]), version, row.text or ""
                )
            )
    if len(docs) != len(rows) or len(rows) != 1058:
        raise ValueError("unverified_public_membership")
    results = {}
    for mode in ("lexical", "dense", "hybrid"):
        if mode != "lexical" and encoder is None:
            continue
        start = perf_counter()
        index = CandidateIndex({"dataset": DEMO_SHA}, docs, mode, encoder)
        t = perf_counter()
        returned = index.search("Мне пришлось ждать готовый заказ", 10)
        times = []
        for _ in range(5):
            tick = perf_counter()
            index.search("Мне пришлось ждать готовый заказ", 10)
            times.append(perf_counter() - tick)
        results[mode] = dict(
            build_s=t - start,
            first_query_s=perf_counter() - t - sum(times),
            warm_queries_s=times,
            diagnostics=index.diagnostics,
            reviews=len(rows),
            eligible=index.eligible,
            fragments=len(index.spans),
            distinct_returned=returned["retrieved_review_count"],
        )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minilm", action="store_true")
    parser.add_argument(
        "--public", action="store_true", help="also profile the SHA-verified packaged demo"
    )
    args = parser.parse_args()
    start = perf_counter()
    encoder = None
    status = "NOT_REQUESTED"
    if args.minilm:
        try:
            encoder = offline_encoder()
        except ValueError as e:
            status = str(e)
    report = dict(model_load_s=perf_counter() - start, splits={})
    for split, sha, docs, queries in inputs():
        rows = [Document(i, text_sha(t), t) for i, t in docs.items()]
        methods = {}
        for name, mode, whole in [
            ("whole_lexical", "lexical", True),
            ("whole_dense", "dense", True),
            ("fragment_lexical", "lexical", False),
            ("fragment_dense", "dense", False),
            ("fusion", "hybrid", False),
        ]:
            if mode != "lexical" and encoder is None:
                methods[name] = dict(status=status)
                continue
            t = perf_counter()
            index = CandidateIndex(dict(dataset=sha, split=split), rows, mode, encoder, whole=whole)
            method = dict(
                status="RUN",
                documents=len(rows),
                fragments=len(index.spans),
                excluded=len(rows) - index.eligible,
                build_s=perf_counter() - t,
                truncated=index.truncated if mode != "lexical" else None,
                index=index.manifest,
                diagnostics=index.diagnostics,
                queries={},
            )
            for q, role, query, labels in queries:
                t = perf_counter()
                hits = index.search(query, min(50, len(docs)), role)["candidates"]
                method["queries"][q] = dict(
                    role=role,
                    query_s=perf_counter() - t,
                    ranked=[[h["version_id"], h["start"], h["end"], h["channels"]] for h in hits],
                    at_k={k: evaluate(list(docs), hits, labels, k) for k in KS},
                )
            methods[name] = method
        report["splits"][split] = methods
    if args.public:
        report["public_profile"] = public_profile(encoder)
    report["resources"] = resources()
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    main()
