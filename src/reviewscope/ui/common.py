"""Shared UI helpers: store/engine access, explainability rendering.

Only rendering helpers import ``streamlit``; the rest is plain Python so page
functions stay testable without a running server.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.models.scores import ConfidenceLevel, ScoreResult
from reviewscope.storage import DuckDBStore

DEFAULT_DB_PATH = "data/reviewscope.duckdb"

_BADGE = {
    ConfidenceLevel.HIGH: ":red-background",
    ConfidenceLevel.MEDIUM: ":orange-background",
    ConfidenceLevel.LOW: ":green-background",
}


@dataclass(frozen=True)
class FilterState:
    """Sidebar filters applied to review-level views (SPEC.md §26)."""

    date_from: str | None = None
    date_to: str | None = None
    ratings: tuple[int, ...] = ()
    categories: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = ()

    def active(self) -> bool:
        return bool(self.date_from or self.date_to or self.ratings or self.categories or self.reviewers)


@lru_cache(maxsize=1)
def _store(db_path: str) -> DuckDBStore:
    return DuckDBStore(db_path=db_path, read_only=True)


@lru_cache(maxsize=8)
def _engine(db_path: str) -> AnalysisEngine:
    return AnalysisEngine(_store(db_path))


def db_exists(db_path: str = DEFAULT_DB_PATH) -> bool:
    return Path(db_path).exists()


def get_store(db_path: str = DEFAULT_DB_PATH) -> DuckDBStore:
    return _store(db_path)


def get_engine(db_path: str = DEFAULT_DB_PATH) -> AnalysisEngine:
    return _engine(db_path)


def confidence_badge(level: ConfidenceLevel | str) -> str:
    state = str(level)
    color = "red" if state == "HIGH" else "orange" if state == "MEDIUM" else "green"
    return f":{color}[{state}]"


def explain(result: ScoreResult, *, title: str | None = None) -> None:
    """Render one explainable score: value, band, signals, counter-signals (§36).

    The UI must show all four fields and must never silently drop counter-signals.
    """
    import streamlit as st

    heading = title or result.name
    st.markdown(f"**{heading}** — {result.rendered_value()} · {confidence_badge(result.confidence)}")
    if result.signals:
        for s in result.signals:
            st.markdown(f"- :green[+ {s}]")
    else:
        st.markdown("- no positive signals")
    if result.counter_signals:
        st.markdown("**Counter-signals:**")
        for s in result.counter_signals:
            st.markdown(f"- :red[− {s}]")
    if result.details:
        st.caption(" · ".join(f"{k}: {v}" for k, v in result.details.items()))


def filter_reviews(reviews: list, flt: FilterState) -> list:
    """Apply sidebar filters to a review list (no store round-trip)."""
    if not flt.active():
        return reviews
    out = []
    for r in reviews:
        day = (r.published_at or "")[:10]
        if flt.date_from and day and day < flt.date_from:
            continue
        if flt.date_to and day and day > flt.date_to:
            continue
        if flt.ratings and r.rating is not None and r.rating not in flt.ratings:
            continue
        if flt.categories and r.place_category and r.place_category not in flt.categories:
            continue
        if flt.reviewers and r.reviewer_id not in flt.reviewers:
            continue
        out.append(r)
    return out


def reviews_frame(reviews: list) -> pd.DataFrame:
    return pd.DataFrame([r.model_dump() for r in reviews])
