from collections import Counter

import numpy as np
import pytest

from reviewscope.aspect_decisions import (
    LABELS,
    PROFILE,
    DecisionScorer,
    aggregate,
    definitions,
    windows,
)
from scripts.benchmark_aspect_decisions import binary, load_rows


def test_freeze_membership_offsets_and_metadata_are_not_features():
    rows = load_rows({"train", "cal", "test"})
    assert Counter(r["split"] for r in rows) == {"train": 55, "cal": 42, "test": 66}
    stars = [r for r in rows if r["stars"] is not None]
    assert any(r["stars"] == 5 and r["labels"]["queue"]["outcome"] == "complaint" for r in stars)
    assert any(
        r["stars"] == 1 and all(v["outcome"] == "not_mentioned" for v in r["labels"].values())
        for r in stars
    )
    assert len({r["family"] for r in rows}) == 163
    assert sum(len(r["labels"]) for r in rows) == 978


def test_all_windows_coordinates_and_counter_context():
    text = "🫖 e\u0301\r\n" + "приём. " * 52 + "хвост"
    spans = list(windows(text))
    assert set(range(len(text))) == {i for a, b in spans for i in range(a, b)}
    assert spans[-1][1] == len(text)
    values = np.zeros((len(spans), len(LABELS)))
    values[:, LABELS.index("not_mentioned")] = 1
    values[0, LABELS.index("complaint")] = 3
    values[-1, LABELS.index("positive")] = 4
    result = aggregate(spans, values, 0.0)
    assert result == dict(
        outcome="mixed",
        reason="DISJOINT_STANCES",
        score=2.0,
        start=0,
        end=160,
        counter_start=spans[-1][0],
        counter_end=len(text),
    )
    values[:, LABELS.index("complaint")] = 0
    values[:, LABELS.index("positive")] = 0
    absent = aggregate(spans, values, 0.0)
    assert absent["outcome"] == "not_mentioned" and absent["start"] is None
    low = aggregate(spans, values, 1.1)
    assert low["outcome"] == "topic_uncertain" and low["start"] == 0
    values[0, 0] = float("inf")
    with pytest.raises(ValueError, match="INVALID_MODEL_OUTPUT"):
        aggregate(spans, values, 0.0)


def test_portable_real_head_and_definition_scope():
    payload = definitions()["queue"]
    frozen = {"payload": payload, "origin": "manual"}
    scorer = DecisionScorer(frozen, PROFILE)
    assert scorer.threshold_set == "calibrated-v1" and len(scorer.profile_sha256) == 64
    text = "К оформившему заказ кассиру смогли подойти лишь через сорок минут."
    assert scorer.score(text) == scorer.score(text)
    known = "Где тархун из набора? Оплаченную бутылку забыли вложить."
    missing = DecisionScorer({"payload": definitions()["missing"], "origin": "manual"}, PROFILE)
    assert missing.score(known) == dict(
        outcome="complaint",
        reason="MODEL_CONTEXT",
        start=0,
        end=len(known),
        score=pytest.approx(0.07314328408744131, abs=1.0e-10),
        counter_start=None,
        counter_end=None,
    )
    for invalid in (
        dict(frozen, payload=payload | {"exclude": ["Иное"]}),
        dict(frozen, origin="import"),
    ):
        with pytest.raises(ValueError, match="UNSUPPORTED_DEFINITION"):
            DecisionScorer(invalid, PROFILE)
    with pytest.raises(ValueError, match="UNSUPPORTED_PROFILE"):
        DecisionScorer(frozen, "new")
    with pytest.raises(ValueError, match="EXCLUDED_BLANK"):
        scorer.score(" \r\n")


def test_binary_oracle_counts_positive_abstention_and_preserves_unknown():
    cases = [
        ("complaint", "unclear"),
        ("mixed", "complaint"),
        ("denial", "complaint"),
        ("unclear", "complaint"),
        ("topic_uncertain", "not_mentioned"),
        ("unknown", "complaint"),
        ("not_mentioned", "not_mentioned"),
        ("denial", "unclear"),
    ]
    assert binary([dict(gold=g, outcome=p) for g, p in cases]) == dict(
        tp=1, fp=1, fn=1, tn=1, abstained_negative=1, known=5, precision=0.5, recall=0.5, f1=0.5
    )
    assert binary([])["recall"] is None


def test_resource_fixtures_preserve_repeated_positions_and_public_membership():
    from reviewscope.import_preview.contract import MAX_BYTES
    from scripts.profile_aspect_decisions import workload

    repeated = workload("repeated")
    assert len(repeated) == 10000 and len(set(repeated)) == 2
    assert 8 + sum(7 + len(text) for text in repeated) == MAX_BYTES
    public = workload("public-demo")
    assert len(public) == sum(bool(t.strip()) for t in public) == 1058
    assert len(set(public)) == 1033
