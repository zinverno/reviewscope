"""Data Quality page (SPEC.md §31): missing fields, duplicate ids, invalid dates."""

from __future__ import annotations

from datetime import date

import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState


def _quality_report(store: DuckDBStore) -> dict[str, int | list]:
    rows = store.fetch_reviews()
    total = len(rows)
    missing_text = sum(1 for r in rows if not (r.text or "").strip())
    missing_rating = sum(1 for r in rows if r.rating is None)
    missing_dates_v = sum(1 for r in rows if not r.published_at)
    missing_reviewer = sum(1 for r in rows if not r.reviewer_id)
    missing_category = sum(1 for r in rows if not r.place_category)
    missing_coords = sum(1 for r in rows if r.latitude is None or r.longitude is None)

    invalid_dates = 0
    for r in rows:
        if r.published_at:
            try:
                date.fromisoformat(r.published_at[:10])
            except ValueError:
                invalid_dates += 1

    seen: dict[str, int] = {}
    dup_ids: list[str] = []
    for r in rows:
        seen[r.review_id] = seen.get(r.review_id, 0) + 1
    dup_ids = [rid for rid, n in seen.items() if n > 1]

    return {
        "total": total,
        "missing_text": missing_text,
        "missing_rating": missing_rating,
        "missing_dates": missing_dates_v,
        "missing_reviewer": missing_reviewer,
        "missing_category": missing_category,
        "missing_coords": missing_coords,
        "invalid_dates": invalid_dates,
        "duplicate_ids": dup_ids,
    }


def render_data_quality_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    st.header("Data Quality")

    report = _quality_report(store)
    fields = [
        ("Missing text", "missing_text"),
        ("Missing rating", "missing_rating"),
        ("Missing dates", "missing_dates"),
        ("Missing reviewer", "missing_reviewer"),
        ("Missing category", "missing_category"),
        ("Missing coordinates", "missing_coords"),
        ("Invalid dates", "invalid_dates"),
    ]

    col1, col2, col3 = st.columns(3)
    col1.metric("Total reviews", report["total"])
    col2.metric("Duplicate ids", len(report["duplicate_ids"]))
    col3.metric("Invalid dates", report["invalid_dates"])

    st.markdown("---")
    tbl = {label: report[key] for label, key in fields}
    tbl["Duplicate ids"] = len(report["duplicate_ids"])
    import pandas as pd

    st.dataframe(
        pd.DataFrame({"Check": list(tbl), "Count": list(tbl.values())}),
        width="stretch",
    )

    if report["duplicate_ids"]:
        st.warning("Duplicate review ids found: " + ", ".join(report["duplicate_ids"][:10]))
