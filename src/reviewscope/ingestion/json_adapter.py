"""Tolerant JSON data source adapter (SPEC.md §6).

Accepts:

* a top-level JSON array of review objects;
* an object with a ``"reviews"`` array;
* an object mapping ``review_id -> review object``.

Parsing is defensive: non-object records are counted as skipped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reviewscope.ingestion.base import DataSource, LoadResult
from reviewscope.ingestion.normalize import ValidationReport, normalize_review


class JSONAdapter(DataSource):
    """Load normalized reviews from a JSON file."""

    label = "json"

    @staticmethod
    def _parse_document(path: str | Path) -> list[dict[str, Any]]:
        raw = Path(path).read_bytes()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = json.loads(raw.decode("utf-8-sig"))
        if isinstance(data, dict):
            if isinstance(data.get("reviews"), list):
                return [item for item in data["reviews"] if isinstance(item, dict)]
            return [item for item in data.values() if isinstance(item, dict)]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        raise ValueError(f"unexpected JSON structure in {path!s}")

    def load(self, path: str | Path, **kwargs: object) -> LoadResult:
        records = self._parse_document(path)
        # Deduplicate records sharing a review_id by keeping the first.
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for record in records:
            rid = record.get("review_id") or record.get("id")
            if rid is not None:
                key = str(rid)
                if key in seen:
                    continue
                seen.add(key)
            rows.append(record)

        report = ValidationReport(total_rows=len(records))
        reviews = []
        for index, row in enumerate(rows):
            review, warnings = normalize_review(row)
            for message in warnings:
                report.add_warning(index + 1, message)
            if review is None:
                report.skipped += 1
                continue
            reviews.append(review)
        report.valid = len(reviews)
        return LoadResult(reviews=reviews, report=report)
