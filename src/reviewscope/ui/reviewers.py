"""Reviewers page (SPEC.md §24): per-reviewer metrics and breakdowns."""

from __future__ import annotations

import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState


def _frame_for(metrics: list) -> None:
    import pandas as pd

    rows = [
        {
            "reviewer_id": m.reviewer_id,
            "reviews": m.review_count,
            "active_period_days": m.active_period_days,
            "categories": m.category_count,
            "cities": m.city_count,
            "rating_mean": round(m.rating_mean, 2) if m.rating_mean else None,
            "rating_std": round(m.rating_std, 2) if m.rating_std else None,
            "rating_entropy": round(m.rating_entropy, 2) if m.rating_entropy else None,
            "text_length_mean": round(m.text_length_mean, 1) if m.text_length_mean else None,
            "specificity_mean": round(m.specificity_mean, 1) if m.specificity_mean else None,
            "review_diversity": round(m.review_diversity, 2) if m.review_diversity else None,
            "review_consistency": round(m.review_consistency, 2) if m.review_consistency else None,
            "duplicate_ratio": m.duplicate_ratio,
            "template_ratio": m.template_ratio,
        }
        for m in metrics
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch")


def render_reviewers_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)
    st.header("Reviewers")

    metrics = p.reviewer_overview or []
    _frame_for(metrics)

    st.markdown("---")
    reviewer_ids = sorted({r.reviewer_id for r in p.reviews})
    selected = st.selectbox("Reviewer", reviewer_ids)
    if not selected:
        return

    history = engine._history_by_reviewer().get(selected, [])
    st.subheader(f"Reviewer {selected}")
    st.markdown(f"**History:** {len(history)} reviews across {len({r.place_id for r in history})} places")

    if history:
        cats: dict[str, int] = {}
        cities: dict[str, int] = {}
        for r in history:
            cats[r.place_category or "unknown"] = cats.get(r.place_category or "unknown", 0) + 1
            cities[r.city or r.region or "unknown"] = cities.get(r.city or r.region or "unknown", 0) + 1
        st.markdown("**Categories:** " + ", ".join(f"{k} ({v})" for k, v in sorted(cats.items())))
        st.markdown("**Cities:** " + ", ".join(f"{k} ({v})" for k, v in sorted(cities.items())))

    st.markdown("**Reviews in this place:**")
    for r in [r for r in p.reviews if r.reviewer_id == selected][:10]:
        st.markdown(f"- {r.rating}★ · {r.published_at[:10] if r.published_at else '—'} — {r.text_or_empty()[:140]}")
