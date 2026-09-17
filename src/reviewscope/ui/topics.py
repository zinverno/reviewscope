"""Topics page (SPEC.md §30): semantic clusters with representative reviews."""

from __future__ import annotations

import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState


def render_topics_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)
    st.header("Topics")

    if not p.clusters:
        st.info("No semantic clusters detected for this place.")
        return

    by_id = {r.review_id: r for r in p.reviews}

    for c in sorted(p.clusters, key=lambda c: len(c.review_ids), reverse=True):
        reps = [by_id[rid] for rid in c.review_ids[:3] if rid in by_id]
        with st.expander(
            f"Cluster {c.cluster_id} · {len(c.review_ids)} reviews · avg {c.avg_rating:.2f} · "
            f"{c.date_min} → {c.date_max}",
        ):
            st.markdown("**Keywords:** " + ("; ".join(sorted(set(c.representative_phrases))[:10]) or "—"))
            st.markdown("**Representative reviews:**")
            for r in reps:
                st.markdown(f"- {r.rating}★ · `{r.reviewer_id}` — {r.text_or_empty()[:160]}")
            if c.signals:
                st.markdown("**Signals:** " + "; ".join(c.signals))
            if c.counter_signals:
                st.markdown("**Counter-signals:** " + "; ".join(c.counter_signals))
