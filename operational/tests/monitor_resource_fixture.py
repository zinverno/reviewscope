"""Frozen reimport composition for both PR-22 10k corpora."""

import csv
from pathlib import Path

from operational.tests.test_decision_resources import resource_input
from operational.tests.test_deliveries import inputs
from reviewscope.import_preview.contract import MAX_BYTES


def resource_pair(directory, profile):
    before = directory / "A"
    after = directory / "B"
    before.mkdir()
    after.mkdir()
    original = resource_input(before, profile)
    original_path = Path(original[original.index("--input") + 1])
    with original_path.open(newline="", encoding="utf-8") as stream:
        header, *rows = csv.reader(stream)
    assert len(rows) == 10000 and original_path.stat().st_size == MAX_BYTES
    modified = after / "input.csv"
    with modified.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        for i, (ident, text) in enumerate(rows):
            if 9000 <= i < 9400:
                text = "Z" + text[1:]
            elif 9400 <= i < 9800:
                ident = "M" + ident[1:] if len(ident) == 5 else "M" + ident
            elif i >= 9800:
                ident = ""
            writer.writerow((ident, text))
    assert modified.stat().st_size <= MAX_BYTES
    new = inputs(modified, before / "mapping", before / "passport", "csv")
    return original, new
