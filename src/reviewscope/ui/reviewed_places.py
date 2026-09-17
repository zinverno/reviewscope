"""Reviewed Places page (SPEC.md §25): reviewer timeline map.

Timeline is based on review publication timestamps and does not represent
verified physical movement — the UI states this explicitly.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from plotly import graph_objects as go

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState


def render_reviewed_places_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)
    st.header("Reviewed Places")

    reviewer_ids = sorted({r.reviewer_id for r in p.reviews})
    selected = st.selectbox("Reviewer", reviewer_ids)
    if not selected:
        return

    history = engine._history_by_reviewer().get(selected, [])
    rows = [
        {
            "review_id": r.review_id,
            "place_name": r.place_name or r.place_id,
            "place_category": r.place_category,
            "rating": r.rating,
            "city": r.city,
            "region": r.region,
            "published_at": r.published_at,
            "latitude": r.latitude,
            "longitude": r.longitude,
            "text": r.text_or_empty()[:80],
        }
        for r in history
        if r.latitude is not None and r.longitude is not None
    ]
    if not rows:
        st.info("No reviews with coordinates in this reviewer's history.")
        return

    frame = pd.DataFrame(rows).sort_values("published_at")

    st.caption(
        "Timeline is based on review publication timestamps and does not "
        "represent verified physical movement."
    )
    st.dataframe(
        frame[["published_at", "place_name", "place_category", "rating", "city", "region"]],
        width="stretch",
    )

    lat_center = float(frame["latitude"].mean())
    lon_center = float(frame["longitude"].mean())

    fig = go.Figure(
        go.Scattermap(
            lat=frame["latitude"],
            lon=frame["longitude"],
            mode="markers",
            marker=go.scattermap.Marker(size=10, color=frame["rating"], colorbar=dict(title="Rating"), cmin=1, cmax=5),
            customdata=frame[["published_at", "place_name", "city"]].values,
            hovertemplate=(
                "<b>%{customdata[1]}</b><br>"
                "date: %{customdata[0]}<br>city: %{customdata[2]}<br>"
                "rating: %{marker.color:.1f}<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        map=go.layout.Map(
            style="light",
            center=go.layout.map.Center(lat=lat_center, lon=lon_center),
            zoom=5,
        ),
        height=480,
        margin=dict(l=0, r=0, t=0, b=0),
    )
    st.plotly_chart(fig, width="stretch")
