import hashlib
from uuid import uuid4

import pytest
from fastapi import HTTPException

from reviewscope.access.decisions import Row, verify_evidence


def test_verified_model_windows_reference_unicode_and_independent_counter_context():
    text = "🍒e\u0301\r\nЖдали. В другой день выдали сразу."
    ref = Row(
        version_id=uuid4(),
        content_sha256="a" * 64,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        start=5,
        end=11,
        counter_start=12,
        counter_end=len(text),
        outcome="mixed",
        score=1.5,
        reason="CONTEXT",
    )
    primary, counter = verify_evidence(ref, text)
    assert primary.text == "Ждали." and counter.text == "В другой день выдали сразу."
    for wrong in (
        dict(start=-1),
        dict(counter_end=len(text) + 1),
        dict(counter_start=10),
        dict(outcome="denial"),
    ):
        with pytest.raises(HTTPException):
            verify_evidence(ref.model_copy(update=wrong), text)
    with pytest.raises(HTTPException):
        verify_evidence(ref, text.replace("\r\n", "\n"))
    giant = "🍒" * 2000
    no_span = ref.model_copy(
        update=dict(
            outcome="not_mentioned",
            start=None,
            end=None,
            counter_start=None,
            counter_end=None,
            text_sha256=hashlib.sha256(giant.encode()).hexdigest(),
        )
    )
    preview, counter = verify_evidence(no_span, giant)
    assert counter is None and preview.text == "🍒" * 1024 and preview.length == 2000
