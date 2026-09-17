"""Tolerant CSV data source adapter (SPEC.md §6).

Reads UTF-8 (falling back to cp1251/latin-1), maps column aliases, and never
drops the import on an individual bad row.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

from reviewscope.ingestion.base import DataSource, LoadResult
from reviewscope.ingestion.normalize import ValidationReport, normalize_review

_ENCODINGS = ("utf-8", "utf-8-sig", "cp1251", "latin-1")


class CSVAdapter(DataSource):
    """Load normalized reviews from a CSV file."""

    label = "csv"

    @staticmethod
    def _read_csv(path: str | Path) -> pd.DataFrame:
        """Read a CSV decoding with a tolerant encoding chain."""
        data = Path(path).read_bytes()
        last_error: Exception | None = None
        for encoding in _ENCODINGS:
            try:
                text = data.decode(encoding)
                return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
            except (UnicodeDecodeError, Exception) as exc:  # noqa: BLE001
                last_error = exc
        if isinstance(last_error, UnicodeDecodeError):
            raise ValueError(
                f"cannot decode {path!s}; tried {_ENCODINGS}: {last_error}"
            )
        raise ValueError(f"cannot read CSV {path!s}: {last_error}")

    def load(self, path: str | Path, **kwargs: object) -> LoadResult:
        df = self._read_csv(path)
        report = ValidationReport(total_rows=len(df))
        reviews = []
        for index, raw in enumerate(df.to_dict(orient="records")):
            review, warnings = normalize_review(raw)
            for message in warnings:
                report.add_warning(index + 2, message)
            if review is None:
                report.skipped += 1
                continue
            reviews.append(review)
        report.valid = len(reviews)
        return LoadResult(reviews=reviews, report=report)
