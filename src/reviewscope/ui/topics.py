"""Topics page (SPEC.md §30): semantic clusters as readable topic cards.

Clusters keep their algorithmic identity (keyword phrases, review counts,
average rating, date range) but are presented as human-readable "topic"
cards instead of debug cluster dumps. We never invent a semantic label that
the clustering algorithm did not produce: names are ``Topic N`` plus the
actual representative phrases.
"""

from __future__ import annotations

import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState, filter_reviews, info_state


def _keywords(c) -> str:
    phrases = sorted({p for p in c.representative_phrases if p.strip()})
    if phrases:
        return " · ".join(phrases[:8])
    return "no shared keywords"


def _date_range(c) -> str:
    if c.date_min and c.date_max:
        if c.date_min == c.date_max:
            return f"Reviewed on {c.date_min}"
        return f"{c.date_min} → {c.date_max}"
    return "no review dates on record"


def render_topics_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)
    st.header("Topics")
    st.caption("Groups of reviews that talk about similar things.")

    if not p.reviews:
        info_state(
            "No topics available",
            "There are no reviews for this place in the dataset.",
        )
        return

    if not p.clusters:
        info_state(
            "No clear topic groups",
            "The reviews in this selection do not separate into distinct topic "
            "groups (small selections and very uniform topics often behave this way).",
            hint="Broaden the review selection or check the Overview charts.",
        )
        return

    selected = filter_reviews(p.reviews, flt)
    selected_ids = {r.review_id for r in selected}
    by_id = {r.review_id: r for r in p.reviews}
    total_selected = max(len(selected_ids), 1)

    if flt.active():
        st.caption(
            f"Topics are computed over all reviews of this place; shares reflect "
            f"the {len(selected)} reviews currently shown."
        )

    ordered = sorted(p.clusters, key=lambda c: len(c.review_ids), reverse=True)

    for level, c in enumerate(ordered, start=1):
        members_in_selection = [rid for rid in c.review_ids if rid in selected_ids]
        share = len(members_in_selection) / total_selected * 100

        with st.container(border=True):
            st.markdown(f"**Topic {level}** — {_keywords(c)}")
            st.caption(
                f"{len(c.review_ids)} reviews · {share:.1f}% of selected reviews · "
                f"average rating {c.avg_rating:.2f}"
            )
            st.caption(_date_range(c))

            # Representative reviews chosen by the algorithm when available.
            rep_ids = [rid for rid, _score in c.representative_reviews]

            def _show_reviews(ids: list[str], max_rows: int) -> None:
                rows = 0
                for rid in ids:
                    review = by_id.get(rid)
                    if review is None or not review.text_or_empty().strip():
                        continue
                    rating = f"{review.rating}★" if review.rating is not None else "no rating"
                    when = (review.published_at or "no date")[:10]
                    st.markdown(f"- {rating} · {when} — {review.text_or_empty()[:160]}")
                    rows += 1
                    if rows >= max_rows:
                        break
                if rows == 0:
                    st.caption("No review text on record for this topic.")

            with st.expander("Representative reviews", expanded=False):
                _show_reviews(list(rep_ids), 4)
                if c.signals:
                    st.markdown("**Signals:** " + "; ".join(c.signals))
                if c.counter_signals:
                    st.markdown("**Counter-signals:** " + "; ".join(c.counter_signals))
