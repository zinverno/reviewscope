from itertools import combinations

import numpy as np

from reviewscope.typed_relations import lexical_candidates, neighbors, text_relation
from scripts.freeze_relation_corpus import corpus


def test_pair_symmetry_blank_buckets_and_direct_only():
    docs = {
        "b": "Северная пристань",
        "a": " СЕВЕРНАЯ   пристань ",
        "d": "Северная пристань!",
        "c": "Северная пристань!",
        "empty": "\u2003",
    }
    pairs, summary = lexical_candidates(docs)
    assert (pairs, summary) == lexical_candidates(dict(reversed(list(docs.items()))))
    assert summary["excluded_blank_count"] == 1 and summary["exact_possible_pairs"] == 2
    assert len(pairs) == 6 and all(a < b for a, b in pairs)
    assert pairs["b", "d"]["fuzzy"] > 0.85 and "exact" not in pairs["b", "d"]
    assert text_relation(docs["a"], docs["b"])[0] == "exact_text"
    assert text_relation(" ", "\t") == ("not_evaluated", None)
    ids = ["a", "b", "c"]
    sim = np.array([[1, 0.8, 0.1], [0.8, 1, 0.9], [0.1, 0.9, 1]])
    picked = neighbors(ids, sim, 0.3, 1)
    assert set(picked) == {("a", "b"), ("b", "c")}
    assert set(picked["a", "b"]) == {"a"}
    assert picked == neighbors(ids[::-1], sim[::-1, ::-1], 0.3, 1)


def test_corpus_separation_and_evidence():
    docs, pairs = corpus()
    assert len(docs) == 90 and len(pairs) == 3 * 435
    for a, b in combinations(docs, 2):
        if a["split"] != b["split"]:
            assert (
                a["family"] != b["family"]
                and a["text"].lower().split() != b["text"].lower().split()
            )
    by = {d["id"]: d for d in docs}
    for p in pairs:
        for axis in ("queue", "handover"):
            if p[axis] in {"same_specific_complaint", "opposite_stance"}:
                for i in (p["left"], p["right"]):
                    lo, hi = by[i]["spans"][axis]
                    assert 0 <= lo < hi <= len(by[i]["text"]) and by[i]["text"][lo:hi].strip()
    choices = [p for p in pairs if p["text"] == "near_copy"]
    assert any(p["queue"] == "opposite_stance" for p in choices)
    assert any(p["handover"] == "opposite_stance" for p in choices)


def test_exact_bucket_stops_before_enumeration():
    pairs, report = lexical_candidates({str(i): "одна строка" for i in range(10000)})
    assert not pairs and report["exact_possible_pairs"] == 49995000
    assert (
        report["status"] == "PAIR_BUDGET_LIMIT"
        and report["candidate_pairs_count"] is None
        and report["compared"] == 0
    )
