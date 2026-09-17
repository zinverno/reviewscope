"""Duplicates page (SPEC.md §29): near-identical review groups with filters."""

from __future__ import annotations

import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState


def render_duplicates_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)
    st.header("Duplicates")

    groups = [g for g in p.duplicate_groups if len(g.review_ids) >= 2]
    if not groups:
        st.info("No duplicate groups detected for this place.")
        return

    with st.expander("Filters", expanded=False):
        kinds = st.multiselect(
            "Match types",
            ["exact", "fuzzy", "near", "semantic"],
            default=["exact", "fuzzy", "near", "semantic"],
        )
        min_size = st.slider("Min group size", 2, max(len(g.review_ids) for g in groups), 2)
    sort_by = st.radio("Sort by", ["group size", "similarity"], horizontal=True)

    by_id = {r.review_id: r for r in p.reviews}
    shown = []
    for g in groups:
        if len(g.review_ids) < min_size:
            continue
        counts = {"exact": g.exact_count, "fuzzy": g.fuzzy_count, "near": g.near_count, "semantic": g.semantic_count}
        if not any(counts.get(k, 0) > 0 for k in kinds):
            continue
        shown.append(g)

    shown.sort(key=lambda g: (len(g.review_ids) if sort_by == "group size" else g.avg_similarity), reverse=True)

    for g in shown:
        members = [by_id[rid] for rid in g.review_ids if rid in by_id]
        title = (
            f"Group {g.group_id} · {len(g.review_ids)} reviews · "
            f"similarity {g.avg_similarity:.2f} · "
            f"exact {g.exact_count} / fuzzy {g.fuzzy_count} / near {g.near_count} / semantic {g.semantic_count}"
        )
        with st.expander(title):
            for r in members:
                st.markdown(f"- {r.rating}★ · `{r.reviewer_id}` · {r.published_at} — {r.text_or_empty()[:140]}")
            if g.signals:
                st.markdown("**Signals:** " + "; ".join(g.signals))
            if g.counter_signals:
                st.markdown("**Counter-signals:** " + "; ".join(g.counter_signals))
