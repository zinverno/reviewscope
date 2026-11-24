"""PR-20 authoring boundaries use exact Unicode and strict transport types."""

import pytest
from pydantic import ValidationError

from reviewscope.access.aspects import Create, Payload


def test_preserve_unicode_and_order():
    data = dict(
        name="  Касса  ",
        definition="Очередь до приёма заказа.",
        include=["é", "e\u0301", "😀"],
        exclude=["Очереди не было"],
    )
    assert Payload.model_validate(data).model_dump() == data


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "\u00a0"),
        ("definition", "\ud800"),
        ("definition", "\x00"),
        ("include", []),
        ("exclude", [12]),
        ("include", ["да", "да"]),
        ("include", ["нет"]),
        ("exclude", ["a" * 501]),
        ("include", ["x"] * 11),
    ],
)
def test_refuse_invalid_definition(field, value):
    with pytest.raises(ValidationError):
        Payload.model_validate(
            dict(name="имя", definition="граница", include=["да"], exclude=["нет"]) | {field: value}
        )


def test_model_rejects_unscoped_extra_input():
    with pytest.raises(ValidationError):
        Create.model_validate(
            dict(
                payload={"name": "x", "definition": "y", "include": ["a"], "exclude": ["b"]},
                idempotency_key="00000000-0000-0000-0000-000000000001",
                project_id="from-browser",
            )
        )
