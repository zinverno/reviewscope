"""Offline retrieval protocol regressions, using only authored synthetic labels."""

import importlib.util
import json
from math import log2
from pathlib import Path

import pytest
from pydantic import ValidationError

from reviewscope.validation.retrieval_dataset import Dataset, Judgment, load_dataset
from reviewscope.validation.retrieval_metrics import evaluate_query

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "retrieval_cli", ROOT / "scripts/benchmark_retrieval.py"
)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


@pytest.fixture
def raw():
    return json.loads(cli.FIXTURE.read_text())


def label(rid, grade, stance):
    return Judgment(query_id="q", review_id=rid, relevance=grade, stance=stance)


def test_known_ranking_diagnostics_and_graded_metrics():
    grades = [
        label("a", 1, "denial"),
        label("b", 2, "complaint"),
        label("c", 1, "unclear"),
        label("d", 2, "mixed"),
        label("e", 0, "not_applicable"),
    ]
    labels = {j.review_id: j for j in grades}
    row = evaluate_query(list("abcde"), labels, [1, 3, 5])
    at = row["at_k"]["3"]
    assert at["metrics"] == pytest.approx(
        {
            "recall": 0.5,
            "reciprocal_rank": 0.5,
            "ndcg": (1 + 3 / log2(3) + 1 / 2) / (3 + 3 / log2(3) + 1 / 2),
        }
    )
    assert row["at_k"]["1"]["metrics"]["reciprocal_rank"] == 0
    assert row["at_k"]["5"]["metrics"]["recall"] == 1
    assert at["known_false_positives"] == ["a", "c"] and at["known_false_negatives"] == ["d"]
    assert at["fp_by_reason"] == {"denial": 1, "unclear": 1} and at["mixed_exact_missed"] == 1


def test_unjudged_and_zero_positive_queries():
    labels = {"a": label("a", None, None), "b": label("b", 2, "complaint")}
    row = evaluate_query(["a", "c", "b"], labels, [2])
    at = row["at_k"]["2"]
    assert row["incomplete_judgments"] and row["unknown"] == at["unjudged_in_top"] == 2
    assert at["metrics"] is None and at["known_false_positives"] == []
    assert at["known_false_negatives"] == ["b"]
    none = evaluate_query(["n"], {"n": label("n", 1, "denial")}, [1])
    assert none["known_exact"] == 0 and none["at_k"]["1"]["metrics"] is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["documents"].append(d["documents"][0]), "duplicate"),
        (lambda d: d["queries"].append(d["queries"][0]), "duplicate"),
        (lambda d: d["judgments"].append(d["judgments"][0]), "duplicate"),
        (lambda d: d["judgments"][0].update(review_id="absent"), "unresolved"),
        (lambda d: d["judgments"][0].update(relevance=3), "less than or equal"),
        (lambda d: d["judgments"][0].update(relevance=True), "integer"),
        (lambda d: d["judgments"][0].update(stance="denial"), "grade 2"),
        (lambda d: d["judgments"][0].update(evidence_span="absent"), "substring"),
        (lambda d: d.update(judgments=[]), "at least"),
        (lambda d: d["documents"][0].update(text="  "), "pattern"),
        (lambda d: d["documents"][0].pop("source"), "required"),
        (lambda d: d["queries"][-1].update(exemplar_family="f1"), "overlaps"),
        (lambda d: d["documents"][1].update(split="test", family_id="f1"), "crosses splits"),
        (
            lambda d: d["documents"][1].update(
                split="test", text=d["documents"][0]["text"].upper()
            ),
            "crosses splits",
        ),
        (lambda d: d["documents"][0].update(split="novel_place"), "place IDs"),
        (lambda d: d["documents"][0].update(split="future"), "known dates"),
    ],
)
def test_invalid_bundle(raw, change, message):
    change(raw)
    with pytest.raises(ValidationError, match=message):
        Dataset.model_validate(raw)


def test_text_and_fingerprint(tmp_path, raw):
    raw["documents"][0]["text"] = "  " + raw["documents"][0]["text"] + "\n"
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="fingerprint"):
        load_dataset(path)
    unfrozen = load_dataset(path, check_fingerprint=False)
    assert unfrozen.documents[0].text == raw["documents"][0]["text"]
    raw["manifest"]["fingerprint"] = unfrozen.fingerprint()
    path.write_text(json.dumps(raw))
    assert load_dataset(path).fingerprint() == unfrozen.fingerprint()


def test_validation_of_future_and_novel_are_independent(raw):
    for d in raw["documents"]:
        d.update(period="2025-03-01", place_id="known")
    raw["documents"][1].update(split="future", period="2025-04-01")
    raw["documents"][2].update(split="novel_place", period=None, place_id="new")
    Dataset.model_validate(raw)
    raw["documents"][1]["period"] = "2025-02-01"
    with pytest.raises(ValidationError, match="future"):
        Dataset.model_validate(raw)


def test_cli_repeatable_offline_and_mode_coverage(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "OUTPUT", tmp_path / "validation_data/benchmarks")
    monkeypatch.setattr(cli, "local_minilm", lambda: pytest.fail("default loaded model"))
    reports = []
    for _ in range(2):
        assert cli.main([]) == 0
        path = tmp_path / capsys.readouterr().out.strip()
        reports.append(json.loads(path.read_text()))
    first, second = [r["methods"]["tfidf"] for r in reports]
    assert first["queries"] == second["queries"] and first["role_ids"] == second["role_ids"]
    assert len(set(first["role_ids"].values())) == 3
    assert first["cutoffs"] == [5, 10]
    direct = first["summary_by_mode"]["query_to_passage"]
    assert (direct["queries"], direct["fully_judged"], direct["eligible_queries"]) == (3, 2, 2)
    assert direct["with_known_exact"] == 3 and direct["incomplete_judgments"] == 1
    assert first["summary_by_mode"]["text_to_text"]["eligible_queries"] == 1
    assert first["queries"]["q-change"]["at_k"]["5"]["metrics"] is None
    assert reports[0]["kind"] == "synthetic"


def test_missing_weights_produce_no_dense_metrics(monkeypatch):
    monkeypatch.setattr(cli, "local_minilm", lambda: (None, "NOT_RUN_MISSING_WEIGHTS"))
    assert cli.benchmark(load_dataset(cli.FIXTURE), "dev", "minilm") == {
        "status": "NOT_RUN_MISSING_WEIGHTS"
    }


def test_private_holdout_uses_frozen_plan(tmp_path, raw, capsys, monkeypatch):
    data = raw
    data["manifest"].update(kind="private", annotation_source="independent_human")
    for d in data["documents"]:
        d["split"] = "test"
    data["manifest"]["fingerprint"] = Dataset.model_validate(data).fingerprint()
    path = tmp_path / "synthetic-test-of-private-format.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(cli, "ROOT", tmp_path / "checkout")
    assert cli.main(["--dataset", str(path), "--split", "test"]) == 2
    assert "method plan" in capsys.readouterr().err


def test_large_cutoffs_and_label_independent_ranking(raw):
    for i in range(40):
        raw["documents"].append(
            {
                **raw["documents"][0],
                "review_id": f"extra{i}",
                "family_id": f"extra{i}",
                "text": f"Другая история {i}",
            }
        )
    first = cli.benchmark(Dataset.model_validate(raw), "dev", "tfidf")
    raw["judgments"][0].update(relevance=1, stance="unclear", evidence_span=None)
    second = cli.benchmark(Dataset.model_validate(raw), "dev", "tfidf")
    assert first["cutoffs"] == [5, 10, 20, 50]
    assert first["encoding_id"] == second["encoding_id"]
    for qid, row in first["queries"].items():
        assert row["at_k"]["50"]["metrics"] is None
        assert [(r["review_id"], r["cosine"]) for r in row["top"]] == [
            (r["review_id"], r["cosine"]) for r in second["queries"][qid]["top"]
        ]
