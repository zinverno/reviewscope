"""Anomalies page (SPEC.md §28): burst events, rating anomalies, evidence."""

from __future__ import annotations

import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState


def render_anomalies_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)

    st.header("Anomalies")

    rows = []
    for e in p.burst_events:
        rows.append(
            {
                "date": e.date.isoformat(),
                "type": "positive burst" if e.ratings.get(5, 0) + e.ratings.get(4, 0) > e.ratings.get(1, 0) + e.ratings.get(2, 0) else "negative burst",
                "severity": e.severity.value,
                "reviews": e.observed,
                "score": round(e.score, 1),
                "expected": round(e.expected, 2),
                "multiplier": round(e.multiplier, 2),
                "z_score": round(e.z_score, 2),
                "kind": "volume",
            }
        )
    for a in p.rating_anomalies:
        rows.append(
            {
                "date": a.date.isoformat(),
                "type": "rating shift",
                "severity": a.severity.value,
                "reviews": sum(a.event_dist.values()),
                "score": round(a.score, 1),
                "expected": 0.0,
                "multiplier": 0.0,
                "z_score": 0.0,
                "kind": "rating",
            }
        )

    if not rows:
        st.info("No anomaly events detected for this place.")
        return

    import pandas as pd

    frame = pd.DataFrame(rows).sort_values(["date", "score"], ascending=[True, False])
    st.dataframe(frame, width="stretch")

    st.markdown("---")
    selected = st.selectbox("Inspect event", frame.index.to_list(), format_func=lambda i: f"{frame.loc[i, 'date']} · {frame.loc[i, 'type']} · {frame.loc[i, 'severity']} · {frame.loc[i, 'score']}")
    row = frame.loc[selected]

    if row["kind"] == "volume":
        event = next(e for e in p.burst_events if e.date.isoformat() == row["date"])
        st.markdown(f"**Evidence:** {event.observed} reviews vs {event.expected:.1f} expected ({event.multiplier:.1f}×)")
        st.markdown("**Signals:** " + ("; ".join(event.signals) or "—"))
        st.markdown("**Counter-signals:** " + ("; ".join(event.counter_signals) or "—"))
    else:
        anomaly = next(a for a in p.rating_anomalies if a.date.isoformat() == row["date"])
        st.markdown(f"**Evidence:** baseline {anomaly.baseline_dist} → event {anomaly.event_dist}")
        st.markdown("**Signals:** " + ("; ".join(anomaly.signals) or "—"))
        st.markdown("**Counter-signals:** " + ("; ".join(anomaly.counter_signals) or "—"))

    st.markdown("**Affected reviews on that date:**")
    day = row["date"]
    affected = [r for r in p.reviews if (r.published_at or "")[:10] == day]
    for r in affected[:15]:
        st.markdown(f"- {r.rating}★ · reviewer `{r.reviewer_id}` — {r.text_or_empty()[:120]}")
    if len(affected) > 15:
        st.caption(f"… and {len(affected) - 15} more")
