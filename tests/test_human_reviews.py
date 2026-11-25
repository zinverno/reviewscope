from uuid import uuid4

import pytest
from pydantic import ValidationError

from reviewscope.access.human_reviews import Append


@pytest.mark.parametrize(
    "label",
    [
        "complaint",
        "mixed",
        "denial",
        "positive",
        "neutral",
        "unclear",
        "not_mentioned",
        "topic_uncertain",
    ],
)
def test_annotation_has_no_machine_score_or_trusted_client_provenance(label):
    body = dict(
        source_id=uuid4(),
        idempotency_key=uuid4(),
        expected_event_id=None,
        action="correct",
        outcome=label,
        reason="Проверен исходный текст",
        spans=[{"start": 0, "end": 1}],
        selection="all",
    )
    assert Append(**body).outcome == label
    for changed in (
        dict(outcome="excluded_blank"),
        dict(actor=uuid4()),
        dict(score=1),
        dict(created_at="now"),
        dict(reason=" \t"),
        dict(reason="a\x00b"),
        dict(reason="\ud800"),
        dict(reason="я" * 301),
        dict(spans=[dict(start=True, end=2)]),
        dict(spans=[dict(start=2, end=2)]),
        dict(selection="sample"),
        dict(plan_id=uuid4()),
        dict(action="dispute"),
        dict(outcome=None),
    ):
        with pytest.raises(ValidationError):
            Append(**(body | changed))
    if label in ("complaint", "mixed"):
        with pytest.raises(ValidationError):
            Append(**(body | {"spans": []}))
    assert Append(**(body | dict(action="dispute", outcome=None, spans=[]))).outcome is None
