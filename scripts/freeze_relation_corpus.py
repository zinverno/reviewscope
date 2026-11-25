"""Expand the authored, split-first paired protocol. Contains no model predictions."""

import gzip
import json
import re
from hashlib import sha256
from itertools import combinations
from pathlib import Path

ROOT = Path("tests/data/relations")
DEFS = json.loads(Path("src/reviewscope/resources/decision-definitions.json").read_text())
ASPECTS = ("queue", "handover")
LONG = ("Во дворе депо рос клён.", "Рядом с буфетом стояла кадка.", "Ветер трепал паруса.")


def encoded(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def aspect_gold(a, b):
    if "?" in (a, b):
        return None
    if a in "MU" or b in "MU":
        return "uncertain"
    if a == b == "C":
        return "same_specific_complaint"
    if {a, b} == {"C", "D"}:
        return "opposite_stance"
    return "no_relation"


def corpus():
    reviews, pairs = [], []
    for line in (ROOT / "authored.tsv").read_text().splitlines():
        if line.startswith("#"):
            continue
        split, family, queue, handover, marked = line.split("|", 4)
        marked = marked.replace(r"\r\n", "\r\n")
        for i, fragment in enumerate(LONG, 1):
            marked = marked.replace("{long" + str(i) + "}", " ".join([fragment] * 40))
        text, spans, pos = "", [], 0
        for match in re.finditer(r"\[([^\]]+)\]", marked):
            text += marked[pos : match.start()]
            spans.append([len(text), len(text) + len(match[1])])
            text += match[1]
            pos = match.end()
        text += marked[pos:]
        prev = [r for r in reviews if r["split"] == split]
        labels = dict(zip(ASPECTS, (queue, handover), strict=True))
        mentioned = [a for a in ASPECTS if labels[a] not in "-?"]
        if len(mentioned) == 2 and queue == handover == "U":
            spans *= 2
        evidence = dict(zip(mentioned, spans, strict=True))
        reviews.append(
            dict(
                id=f"{split}-{len(prev) + 1:02}",
                source=split,
                split=split,
                family=split + "-" + family,
                text=text,
                stances=labels,
                spans=evidence,
            )
        )
    normalized = {}
    for r in reviews:
        norm = " ".join(r["text"].lower().split())
        assert normalized.setdefault(norm, r["split"]) == r["split"]
    for a, b in combinations(reviews, 2):
        if a["split"] != b["split"]:
            continue
        same = a["text"].lower().split() == b["text"].lower().split()
        pair = dict(
            id=a["id"] + "/" + b["id"],
            left=a["id"],
            right=b["id"],
            split=a["split"],
            text="exact_text"
            if same
            else "near_copy"
            if a["family"] == b["family"]
            else "no_relation",
        )
        for aspect in ASPECTS:
            pair[aspect] = aspect_gold(a["stances"][aspect], b["stances"][aspect])
        pairs.append(pair)
    return reviews, pairs


if __name__ == "__main__":
    for name, rows in zip(("reviews", "pairs"), corpus(), strict=True):
        raw = b"".join(encoded(r) + b"\n" for r in rows)
        (ROOT / (name + ".jsonl.gz")).write_bytes(gzip.compress(raw, mtime=0))
    (ROOT / "definitions.json").write_bytes(
        encoded(
            {
                a: dict(
                    version="authored-v1",
                    payload=DEFS[a],
                    digest=sha256(encoded(DEFS[a])).hexdigest(),
                )
                for a in ASPECTS
            }
        )
        + b"\n"
    )
