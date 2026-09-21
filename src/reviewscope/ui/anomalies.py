"""Anomalies page (SPEC.md §28): volume bursts and rating shifts, product-first.

Honest baseline handling: when the rolling median is ~0 the detector reports a
huge "multiplier", but presentation must not show a misleading ratio. Affected
reviews expose lightweight attributes already present in the analysis outputs.
"""

from __future__ import annotations

from datetime import timedelta

import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.config import CONFIG
from reviewscope.storage import DuckDBStore

from .common import (
    FilterState,
    baseline_phrase,
    confidence_badge,
    friendly_counter_signal,
    info_state,
    rating_mix,
    ratio_phrase,
    review_annotation,
    technical_details,
)


def _per_review_maps(p) -> tuple[dict, dict, set[str]]:
    weights: dict[str, float] = {}
    tpl: dict[str, float] = {}
    for i, r in enumerate(p.reviews):
        if i < len(p.review_weights):
            weights[r.review_id] = p.review_weights[i]
        if i < len(p.templated_scores):
            tpl[r.review_id] = p.templated_scores[i].value
    dup_ids = {rid for g in p.duplicate_groups for rid in g.review_ids}
    return weights, tpl, dup_ids


def _group_size_of(review_id: str, p) -> int | None:
    for g in p.duplicate_groups:
        if review_id in g.review_ids:
            return len(g.review_ids)
    return None


def _render_affected(
    reviews: list,
    *,
    weights: dict,
    tpl: dict,
    dup_ids: set[str],
    p,
    limit: int = 8,
) -> None:
    shown = 0
    for r in reviews:
        rating = f"{r.rating}★" if r.rating is not None else "no rating"
        when = (r.published_at or "no date")[:10]
        tags = review_annotation(
            r,
            weight=weights.get(r.review_id),
            in_duplicate_group=r.review_id in dup_ids,
            group_size=_group_size_of(r.review_id, p),
            templated_score=tpl.get(r.review_id),
        )
        tag_text = f" · {' ,'.join(tags)}" if tags else ""
        st.markdown(f"- {rating} · {when} · reviewer `{r.reviewer_id}`{tag_text} — {r.text_or_empty()[:120]}")
        shown += 1
        if shown >= limit:
            break
    if len(reviews) > shown:
        st.caption(f"+{len(reviews) - shown} more affected review(s) — expand the analysis for details.")


def render_anomalies_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)
    st.header("Anomalies & unusual activity")
    st.caption(
        "Unusual review volume and rating shifts flagged for this place. An "
        "anomaly is a statistical signal — it does not confirm intent."
    )

    if not p.reviews:
        info_state("No reviews", "There are no reviews for this place in the dataset.")
        return

    weights, tpl, dup_ids = _per_review_maps(p)

    def day_of(r) -> str:
        return (r.published_at or "")[:10]

    if not p.burst_events and not p.rating_anomalies:
        info_state(
            "No unusual activity detected",
            "Review volume and rating patterns stayed within the expected range "
            "for this place during the analyzed period.",
            hint="Check the Overview page for the overall rating picture.",
        )
        return

    # --- volume bursts --------------------------------------------------------
    if p.burst_events:
        st.markdown("### Unusual review volume")
        for e in p.burst_events:
            with st.container(border=True):
                st.markdown(
                    f"**Unusual review volume** · {e.date.isoformat()} · "
                    f"{confidence_badge(e.severity)}"
                )
                st.markdown(f"- Observed: **{e.observed} reviews**")
                st.markdown(f"- {baseline_phrase(e.expected)}")
                st.markdown(f"- {ratio_phrase(e.expected, e.multiplier)}")
                if e.ratings:
                    st.markdown(f"- Rating mix on the day: {rating_mix(e.ratings)}")
                else:
                    st.markdown("- Rating mix on the day: no ratings on record")

                if e.signals or e.counter_signals:
                    st.markdown("**Evidence**")
                    for s in e.signals:
                        st.markdown(f"- :green[+ {s}]")
                    for c in e.counter_signals:
                        st.markdown(f"- :red[− {friendly_counter_signal(c)}]")

                affected = [r for r in p.reviews if day_of(r) == e.date.isoformat()]
                if affected:
                    with st.expander(f"Reviews on that day ({len(affected)})", expanded=False):
                        _render_affected(affected, weights=weights, tpl=tpl, dup_ids=dup_ids, p=p)
                else:
                    st.caption("No reviews carry the event date in the current dataset.")

                technical_details(
                    [
                        ("Observed volume", str(e.observed)),
                        ("Expected baseline (rolling median)", f"{e.expected:.2f}"),
                        ("Volume multiplier", f"{e.multiplier:.2f}"),
                        ("Modified z-score", f"{e.z_score:.2f}"),
                        ("Baseline spread (MAD)", f"{e.mad:.2f}"),
                        ("Score (volume component)", f"{e.score:.1f}/100"),
                        ("Raw signals", " · ".join(e.signals) or "—"),
                        ("Raw counter-signals", " · ".join(e.counter_signals) or "—"),
                    ],
                    key=f"vol_{e.date.isoformat()}",
                )

    # --- rating shifts --------------------------------------------------------
    if p.rating_anomalies:
        st.markdown("### Rating shifts")
        st.caption(
            "Rating mix during a short window compared with the historical mix. "
            "A shift is a statistical change, not a verdict."
        )
        for a in p.rating_anomalies:
            with st.container(border=True):
                st.markdown(
                    f"**Rating shift** · {a.date.isoformat()} (±1 day window) · "
                    f"{confidence_badge(a.severity)}"
                )
                # Observed volume: reviews in the anomaly event window.
                win = CONFIG.rating_anomaly.event_window_days
                event_low = a.date - timedelta(days=(win - 1) // 2)
                event_high = a.date + timedelta(days=win // 2)
                window_reviews = [
                    r
                    for r in p.reviews
                    if day_of(r)
                    and event_low.isoformat() <= day_of(r) <= event_high.isoformat()
                ]
                st.markdown(f"- Observed volume in window: **{len(window_reviews)} reviews**")
                if a.baseline_dist:
                    st.markdown(
                        "- Baseline mix: "
                        + ", ".join(f"{k}★ {v:.0%}" for k, v in sorted(a.baseline_dist.items()))
                    )
                if a.event_dist:
                    st.markdown(
                        "- Window mix: "
                        + ", ".join(f"{k}★ {v:.0%}" for k, v in sorted(a.event_dist.items()))
                    )
                if a.dominant_shift:
                    st.markdown(f"- {a.dominant_shift}")

                if a.signals or a.counter_signals:
                    st.markdown("**Evidence**")
                    for c in a.counter_signals:
                        st.markdown(f"- :red[− {friendly_counter_signal(c)}]")

                if window_reviews:
                    with st.expander(f"Reviews in the window ({len(window_reviews)})", expanded=False):
                        _render_affected(
                            window_reviews,
                            weights=weights,
                            tpl=tpl,
                            dup_ids=dup_ids,
                            p=p,
                        )
                else:
                    st.caption("No reviews carry dates inside the event window in the current dataset.")

                technical_details(
                    [
                        ("Jensen-Shannon divergence", f"{a.jsd:.3f}"),
                        ("Score (rating component)", f"{a.score:.1f}/100"),
                        ("Shift", a.dominant_shift or "—"),
                        ("Baseline distribution", str(a.baseline_dist) or "—"),
                        ("Event distribution", str(a.event_dist) or "—"),
                        ("Raw signals", " · ".join(a.signals) or "—"),
                        ("Raw counter-signals", " · ".join(a.counter_signals) or "—"),
                    ],
                    key=f"rat_{a.date.isoformat()}",
                )
