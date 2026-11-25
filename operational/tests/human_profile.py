"""Measure authenticated human operations on the real, diverse lexical run."""

import csv
import hashlib
import json
from time import monotonic
from uuid import UUID, uuid4

from operational.tests.test_access import CSRF


def fill_admitted_corpus(file, byte_limit, row_limit):
    """Extend an independent copy of the diverse texts to the import byte boundary."""
    remaining = byte_limit - file.stat().st_size
    assert remaining > 0
    with file.open(newline="", encoding="utf-8") as src:
        header, *rows = csv.reader(src)
    assert len(rows) == len({text for _, text in rows}) == row_limit
    with file.open("w", newline="", encoding="utf-8") as dest:
        writer = csv.writer(dest)
        writer.writerow(header)
        for ordinal, text in rows:
            # ASCII without CSV metacharacters adds exactly one file byte per character.
            context = (
                f" We visited table {ordinal} on the ground floor. The wall menu listed tea and "
                "soup by the cup. The ticket on the tray showed the same order number as the "
                "display above the counter. There was a place to leave the tray by the door."
            )[:remaining]
            writer.writerow((ordinal, text + context))
            remaining -= len(context)
    assert remaining == 0 and file.stat().st_size == byte_limit


def observe(db, web, source, workspace, route, original_ids):
    db.execute(
        "UPDATE browser_admission SET human_review_allowed=true WHERE source_id=%s", (source,)
    )
    human = route + "/human"
    params = {"source_id": str(source)}
    ops = []

    def request(method, url, *, body=None, **query):
        start = monotonic()
        result = (
            web.post(human + url, json=params | body, headers=CSRF)
            if method == "POST"
            else web.get(human + url, params=params | query)
        )
        seconds = monotonic() - start
        assert result.status_code in (200, 201), result.status_code
        assert len(result.content) <= 65536 and seconds < 12
        ops.append(
            dict(
                method=method,
                path=url.split("/")[1],
                offset=query.get("offset", 0),
                bytes=len(result.content),
                seconds=round(seconds, 4),
            )
        )
        return result.json()

    run = UUID(route.rsplit("/", 1)[1])
    seed = uuid4()
    args = dict(seed=str(seed), size=10000, idempotency_key=str(uuid4()))
    plan = request("POST", "/plans", body=args)["id"]
    preimage = b"human-sample-v1" + UUID(str(workspace)).bytes + run.bytes + seed.bytes
    expected = sorted(original_ids, key=lambda v: (hashlib.sha256(preimage + v.bytes).digest(), v))
    actual = db.execute(
        "SELECT version_id FROM human_plan_member WHERE plan_id=%s ORDER BY ordinal", (plan,)
    ).fetchall()
    assert len(expected) == 10000 and [v for (v,) in actual] == expected
    for offset in (0, 0, 9975):
        rows = request("GET", "/queue", mode="sample", plan_id=plan, offset=offset)
        assert rows["eligible"] == rows["total"] == 10000
        assert [r["version_id"] for r in rows["rows"]] == [
            str(v) for v in expected[offset : offset + 25]
        ]
    miss = request("GET", "/queue", outcome="not_mentioned", state="unreviewed")["rows"][0]
    first = request("POST", "/results", body={"idempotency_key": str(uuid4())})["id"]
    url = "/reviews/" + miss["version_id"] + "/events"
    event = request(
        "POST",
        url,
        body=dict(
            idempotency_key=str(uuid4()),
            expected_event_id=None,
            action="correct",
            outcome="complaint",
            reason="Синтетическая проверка механизма",
            spans=[dict(start=0, end=12)],
            selection="all",
        ),
    )
    second = request("POST", "/results", body={"idempotency_key": str(uuid4())})["id"]
    assert request("GET", "/results/" + first)["summary"]["reviewed"] == 0
    assert request("GET", "/results/" + second, offset=9975)["summary"]["complaints"] == 1
    hits = request("GET", "/queue", mode="disagreement")
    assert hits["total"] == 1 and hits["rows"][0]["event_id"] == event["id"]
    print(
        json.dumps(
            dict(
                probe="human-diverse-lexical",
                eligible=len(expected),
                checked_sample_ids=len(actual),
                human_events=1,
                profile="aspect-linear-lexical-v1",
                operations=ops,
            )
        )
    )
