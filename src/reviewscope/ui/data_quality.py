"""Data Quality page (SPEC.md §31): missing fields, duplicate ids, invalid dates.

Checks are grouped by how much they affect the product views. "Complete
records" is our wording for rows that have every key field present and
parseable — it does not mean the review text is verified.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState, has_coordinates

_VALID_KEYS = ("published_at", "rating", "text", "reviewer_id", "place_category", "coordinates")

CHECKS = [
    (
        "Important",
        [
            (
                "Missing coordinates",
                "missing_coords",
                "Review locations and the Reviewed Places map are unavailable for these records.",
            ),
            (
                "Missing reviewer identity",
                "missing_reviewer",
                "Reviewer-history analytics cannot attribute these records.",
            ),
            (
                "Duplicate review ids",
                "duplicate_ids",
                "Multiple stored rows share one review id; counts may aggregate them together.",
            ),
        ],
    ),
    (
        "Moderate",
        [
            (
                "Missing dates",
                "missing_dates",
                "Time-series charts and anomaly windows exclude these records.",
            ),
            (
                "Missing rating",
                "missing_rating",
                "Rating statistics and rating-mix views exclude these records.",
            ),
        ],
    ),
    (
        "Minor",
        [
            (
                "Missing text",
                "missing_text",
                "Keyword and topic analysis rely on text; text-less records are skipped there.",
            ),
            (
                "Missing category",
                "missing_category",
                "Category grouping across the dataset is incomplete for these records.",
            ),
            (
                "Invalid dates",
                "invalid_dates",
                "Records with unparseable dates are treated as having no date in the views.",
            ),
        ],
    ),
]


def _quality_report(store: DuckDBStore) -> dict[str, int | list]:
    rows = store.fetch_reviews()
    total = len(rows)
    missing_text = sum(1 for r in rows if not (r.text or "").strip())
    missing_rating = sum(1 for r in rows if r.rating is None)
    missing_dates_v = sum(1 for r in rows if not r.published_at)
    missing_reviewer = sum(1 for r in rows if not r.reviewer_id)
    missing_category = sum(1 for r in rows if not r.place_category)
    missing_coords = sum(1 for r in rows if not has_coordinates(r))

    invalid_dates = 0
    for r in rows:
        if r.published_at:
            try:
                date.fromisoformat(r.published_at[:10])
            except ValueError:
                invalid_dates += 1

    seen: dict[str, int] = {}
    for r in rows:
        seen[r.review_id] = seen.get(r.review_id, 0) + 1
    dup_ids = [rid for rid, n in seen.items() if n > 1]

    complete = 0
    for r in rows:
        ok_date = True
        if r.published_at:
            try:
                date.fromisoformat(r.published_at[:10])
            except ValueError:
                ok_date = False
        if not ok_date or r.rating is None or not (r.text or "").strip():
            continue
        if not r.reviewer_id or not r.place_category:
            continue
        if not has_coordinates(r):
            continue
        complete += 1

    return {
        "total": total,
        "complete": complete,
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
    st.caption(
        "How complete the review records are. This reports technical gaps in "
        "the dataset — it does not judge the reviews themselves."
    )

    report = _quality_report(store)
    total = int(report["total"]) or 1
    issues = sum(int(report[k]) for k in ("missing_text", "missing_rating", "missing_dates", "missing_reviewer", "missing_category", "missing_coords", "invalid_dates")) + len(report["duplicate_ids"])

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total reviews", report["total"])
    col2.metric("Complete records", f"{report['complete']} ({report['complete'] / total * 100:.0f}%)")
    col3.metric("Issues found", issues)
    col4.metric("Distinct reviewer ids", len({r.reviewer_id for r in store.fetch_reviews()}))
    st.caption('"Complete records" means every key field is present and parseable; it does not verify the review text.')

    st.markdown("---")

    any_issue = False
    for group_name, checks in CHECKS:
        entries = [(label, report[key], impact) for label, key, impact in checks]
        count = 0
        for _label, value, _imp in entries:
            count += len(value) if isinstance(value, list) else int(value)
        with st.expander(f"{group_name} — {count} affected record(s)", expanded=group_name == "Important"):
            rows = []
            for label, value, impact in entries:
                if isinstance(value, list):
                    count_text = f"{len(value)}"
                    detail = ", ".join(value[:5]) + ("…" if len(value) > 5 else "") if value else "—"
                else:
                    count_text = f"{value}"
                    detail = f"{value / total * 100:.1f}% of records"
                rows.append({"Issue": label, "Records": count_text, "Share / detail": detail, "Impact": impact})
            st.dataframe(pd.DataFrame(rows), width="stretch")
        any_issue = any_issue or bool(count)

    if not any_issue:
        st.success("No data-quality issues detected for the current dataset.")
