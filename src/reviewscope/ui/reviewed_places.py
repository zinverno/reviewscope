"""Reviewed Places page (SPEC.md §25): reviewer timeline map.

Timeline is based on review publication timestamps and does not represent
verified physical movement — the UI states this explicitly. The map zoom
adapts to how spread out the reviewer's locations are.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState, has_coordinates, info_state


def _adaptive_zoom(frame: pd.DataFrame) -> int:
    """Zoom in for tightly clustered data, out for scattered locations."""
    n_locations = frame[["latitude", "longitude"]].drop_duplicates().shape[0]
    if n_locations == 1:
        return 11
    if n_locations <= 5:
        return 8
    return 5


def render_reviewed_places_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)
    st.header("Reviewed Places")
    st.caption(
        "Where a reviewer left reviews, mapped from the coordinates stored in "
        "the review metadata."
    )
    st.caption(
        "Keep in mind: locations come from review timestamps and coordinates. "
        "They do not represent verified physical movement, and no home, work, "
        "or travel route is inferred."
    )

    reviewer_ids = sorted({r.reviewer_id for r in p.reviews})
    if not reviewer_ids:
        info_state(
            "No reviewers",
            "There are no reviews for this place in the dataset.",
        )
        return

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
        if has_coordinates(r)
    ]
    if not rows:
        info_state(
            "No mapped locations",
            "No reviews with coordinates exist in this reviewer's history.",
            hint="The Reviewed Places map needs coordinates; they can be absent in the source data.",
        )
        return

    frame = pd.DataFrame(rows).sort_values("published_at")
    n_locations = frame[["latitude", "longitude"]].drop_duplicates().shape[0]

    st.markdown(
        f"**{selected}** — {len(frame)} mapped review{'s' if len(frame) != 1 else ''} "
        f"across {n_locations} location{'s' if n_locations != 1 else ''}"
    )

    st.caption("Timeline is based on review publication timestamps and does not represent verified physical movement.")
    st.dataframe(
        frame[["published_at", "place_name", "place_category", "rating", "city", "region", "text"]],
        width="stretch",
        hide_index=True,
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
            zoom=_adaptive_zoom(frame),
        ),
        height=480,
        margin=dict(l=0, r=0, t=0, b=0),
    )
    st.plotly_chart(fig, width="stretch")
