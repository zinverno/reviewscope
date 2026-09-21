"""Duplicates page (SPEC.md §29): repeated-text review groups, product-first.

Pair-vs-review semantics are made explicit: fuzzy / near / semantic counts are
pair counts, identical-text counts are review counts. Interpretation stays
neutral ("repeated review pattern", "high textual similarity") and never
asserts fraud.
"""

from __future__ import annotations

import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState, info_state

_DETECTION_KINDS = ("exact", "fuzzy", "near", "semantic")


def _group_stats(members: list) -> tuple[float | None, list[str], int]:
    ratings = [m.rating for m in members if m.rating is not None]
    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    when = sorted({(m.published_at or "")[:10] for m in members if m.published_at})
    distinct_days = len(when)
    n = len(members)
    concentration = 1.0
    if n > 1 and distinct_days > 1:
        concentration = 1.0 - (distinct_days - 1) / (n - 1)
    return avg_rating, when, round(max(0.0, min(1.0, concentration)), 3)


def _interpretation(g, avg_rating: float | None, concentration: float) -> str:
    if g.exact_count >= 2 and g.exact_count >= len(g.review_ids) * 0.5:
        return "Repeated review pattern — several reviews share identical text."
    if g.exact_count >= 2:
        return "Repeated review pattern — identical text plus close variants."
    if g.avg_similarity >= 0.9:
        return "High textual similarity — reviews read as near-copies of each other."
    if g.avg_similarity >= 0.8:
        return "High textual similarity across the group."
    if concentration >= 0.9:
        return "Similar reviews concentrated in a short time frame."
    return "Semantically similar reviews grouped together."


def render_duplicates_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)
    st.header("Duplicates & repeated text")
    st.caption(
        "Groups of reviews whose text is identical or near-identical. This "
        "page reports what the detectors found; it does not assert fraud."
    )

    groups = [g for g in p.duplicate_groups if len(g.review_ids) >= 2]
    by_id = {r.review_id: r for r in p.reviews}

    if not groups:
        info_state(
            "No repeated-text groups",
            "No reviews for this place matched at any detection level.",
            hint="A clean result — most review text here looks unique.",
        )
        return

    members_all = {rid for g in groups for rid in g.review_ids}
    involved = len(members_all & set(by_id))
    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    col1.metric("Repeated-text groups", len(groups))
    col2.metric("Reviews involved", involved)
    col3.metric("Share of place reviews", f"{involved / max(len(p.reviews), 1) * 100:.1f}%")

    # --- filters -------------------------------------------------------------
    with st.expander("Filter groups", expanded=False):
        min_size = st.slider(
            "Minimum group size",
            2,
            max(len(g.review_ids) for g in groups),
            2,
            help="Groups with at least this many reviews.",
        )
        kinds = st.multiselect(
            "Detection levels to include",
            _DETECTION_KINDS,
            default=list(_DETECTION_KINDS),
            help="Include groups that matched any of these detection levels.",
        )
        min_similarity = st.slider(
            "Minimum average similarity",
            0.0,
            1.0,
            0.0,
            0.05,
            help="Only show groups whose average pair similarity is at least this value.",
        )
        min_concentration = st.slider(
            "Minimum date concentration",
            0.0,
            1.0,
            0.0,
            0.05,
            help="1.0 = all reviews in the group were published on the same day.",
        )
    sort_by = st.radio(
        "Sort groups by",
        ["group size", "average similarity", "date concentration"],
        horizontal=True,
    )

    # --- build the display list ----------------------------------------------
    shown = []
    for g in groups:
        if len(g.review_ids) < min_size:
            continue
        counts = {
            "exact": g.exact_count,
            "fuzzy": g.fuzzy_count,
            "near": g.near_count,
            "semantic": g.semantic_count,
        }
        if not any(counts.get(k, 0) > 0 for k in kinds):
            continue
        members = [by_id[rid] for rid in g.review_ids if rid in by_id]
        if not members:
            continue
        avg_rating, when, concentration = _group_stats(members)
        if g.avg_similarity < min_similarity:
            continue
        if concentration < min_concentration:
            continue
        shown.append((g, members, avg_rating, when, concentration))

    if not shown:
        st.info("No repeated-text groups match the current filters.")
        return

    if sort_by == "average similarity":
        shown.sort(key=lambda t: t[0].avg_similarity, reverse=True)
    elif sort_by == "date concentration":
        shown.sort(key=lambda t: t[4], reverse=True)
    else:
        shown.sort(key=lambda t: len(t[1]), reverse=True)

    st.caption(f"{len(shown)} group{'s' if len(shown) != 1 else ''} shown.")

    # --- group cards -----------------------------------------------------------
    for g, members, avg_rating, when, concentration in shown:
        with st.container(border=True):
            st.markdown(f"**Repeated-text group** · {len(members)} reviews")
            stats = []
            stats.append(f"similarity {g.avg_similarity:.2f}")
            if avg_rating is not None:
                stats.append(f"avg rating {avg_rating:.2f}")
            stats.append(f"date spread {' → '.join([when[0], when[-1]]) if len(when) > 1 else (when[0] if when else 'no dates')}")
            stats.append(f"date concentration {concentration:.0%}")
            st.caption(" · ".join(stats))
            st.markdown(_interpretation(g, avg_rating, concentration))

            with st.expander("Review texts in this group", expanded=False):
                for m in members:
                    rating = f"{m.rating}★" if m.rating is not None else "no rating"
                    when_m = (m.published_at or "no date")[:10]
                    st.markdown(f"- {rating} · {when_m} · reviewer `{m.reviewer_id}` — {m.text_or_empty()[:160]}")
                if g.signals:
                    st.markdown("**Signals:** " + "; ".join(g.signals))
                if g.counter_signals:
                    st.markdown("**Counter-signals:** " + "; ".join(g.counter_signals))

            with st.expander("Detection breakdown", expanded=False):
                st.caption("Identical-text counts reviews; fuzzy/near/semantic counts count pairs of reviews.")
                rows = [
                    ("Identical text (reviews)", g.exact_count),
                    ("Fuzzy pairs", g.fuzzy_count),
                    ("Near-duplicate pairs", g.near_count),
                    ("Semantic pairs", g.semantic_count),
                ]
                import pandas as pd

                st.dataframe(pd.DataFrame({"Level": [r[0] for r in rows], "Count": [r[1] for r in rows]}), width="stretch")
