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

NO_DATA = "N/A / insufficient history"

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


# ---------------------------------------------------------------------------
# Human-friendly formatting (plain Python, no streamlit import)
# ---------------------------------------------------------------------------


def human_duration(days: int | None) -> str:
    """Render a day count as a short human phrase, or ``NO_DATA`` if unknown."""
    if not days or days <= 0:
        return NO_DATA
    if days == 1:
        return "1 day"
    if days < 30:
        return f"~{days} days"
    if days < 365:
        return f"~{days // 30} months"
    return f"~{days / 365:.1f} years".replace(".0 years", " years")


def pct_text(fraction: float, digits: int = 1) -> str:
    """Format a 0..1 fraction as a percentage string."""
    return f"{fraction * 100:.{digits}f}%"


def has_coordinates(review) -> bool:
    """True when the review carries usable coordinates.

    DuckDB returns missing float cells as ``nan`` rather than ``None``, so the
    plain ``is not None`` check is not enough. ``nan`` coordinates would reach
    Plotly as invalid points and break the map's empty state.
    """
    lat, lon = review.latitude, review.longitude
    if lat is None or lon is None:
        return False
    try:
        import math

        return not (math.isnan(lat) or math.isnan(lon))
    except TypeError:
        return True


def value_or_nodata(value: float, *, threshold: float = 1e-9) -> str:
    """Render a numeric value or ``NO_DATA`` when it carries no information."""
    if value is None or value < threshold:
        return NO_DATA
    return f"{value:g}"


def baseline_phrase(expected: float) -> str:
    """Honest wording for a volume baseline, including the near-zero case.

    The burst detector's rolling median can be exactly 0 (no measurable daily
    rhythm). Presentation must not suggest a false precision there, and a
    ``observed / expected`` ratio would be misleading.
    """
    if expected <= 0.05:
        return "Expected baseline: <1 review/day"
    return f"Expected baseline: ~{expected:.1f} reviews/day"


def ratio_phrase(expected: float, multiplier: float) -> str:
    """Explain the observed/expected ratio without a misleading multiple."""
    if expected <= 0.05:
        return "Observed/expected ratio: not meaningful, baseline is approximately zero"
    return f"Observed volume is about {multiplier:.1f}× the baseline"


def rating_mix(dist: dict[int, int], *, sort: bool = True) -> str:
    """Render a rating histogram as ``5★×12 · 4★×3 ...`` for compact display."""
    items = sorted(dist.items(), reverse=True) if sort else list(dist.items())
    if not items:
        return "no ratings on record"
    return " · ".join(f"{r}★×{n}" for r, n in items)


def _friendly_signal_hints() -> list[tuple[str, str]]:
    """(match-prefix, friendly text) pairs for the most common signals.

    Friendly wording must stay faithful to the underlying signal: it renames
    implementation vocabulary, it never invents a new fact.
    """
    return [
        ("review velocity anomaly", "Unusually concentrated review volume"),
        ("rating distribution anomaly", "Rating mix on the flagged day differs from the usual pattern"),
        ("semantic cluster overlaps a manipulation event window", "A similar-review cluster overlaps the flagged activity window"),
        ("duplicate density", "Repeated review text patterns"),
        ("reviews published in narrow time window", "Reviews concentrated in a short time window"),
        ("templated review fraction", "Templated review text patterns"),
        ("single reviewer accounts for", "One reviewer accounts for a large share of reviews"),
        ("weighted rating is lower", "Weighted rating is below the raw average"),
        ("weighted rating is higher", "Weighted rating is above the raw average"),
        ("of reviews carry low weight", "Many reviews carry reduced weight (low specificity or flagged patterns)"),
        ("of reviews carry high weight", "Many reviews carry elevated weight"),
        ("volume", "Unusually high review volume"),
    ]


def _friendly_counter_hints() -> list[tuple[str, str]]:
    return [
        ("weighted and raw ratings are nearly identical", "The rating barely changes after adjustment"),
        ("very few duplicate reviews", "Very few repeated review texts detected"),
        ("reviews spread across many distinct reviewers", "Reviews come from many different reviewers"),
        ("no single dominant anomaly signal detected", "No combination of signals points to organized activity"),
        ("baseline is volatile", "The baseline itself is noisy, so the spike is closer to normal variability"),
        ("ratings on the event day are mixed", "Ratings on the event day were mixed, not one-sided"),
        ("no positive signals", "No positive signals detected"),
    ]


def friendly_signal(text: str) -> str:
    """Map an internal signal line onto product wording (fallback: as-is)."""
    if not text:
        return text
    for prefix, friendly in _friendly_signal_hints():
        if text.startswith(prefix):
            return friendly
    return text


def friendly_counter_signal(text: str) -> str:
    """Map an internal counter-signal line onto product wording (fallback: as-is)."""
    if not text:
        return text
    for prefix, friendly in _friendly_counter_hints():
        if text.startswith(prefix):
            return friendly
    return text


def grouped_evidence(score: ScoreResult | None) -> tuple[list[str], list[str]]:
    """Return ``(signals, counter_signals)`` rendered in friendly wording."""
    if score is None:
        return [], []
    return [friendly_signal(s) for s in score.signals], [
        friendly_counter_signal(s) for s in score.counter_signals
    ]


# ---------------------------------------------------------------------------
# Rendering helpers (import streamlit lazily)
# ---------------------------------------------------------------------------


def info_state(title: str, body: str, *, hint: str | None = None) -> None:
    """Render a calm, neutral empty/edge state with an explanation."""
    import streamlit as st

    st.info(f"**{title}**  \n{body}")
    if hint:
        st.caption(hint)


def evidence_block(
    signals: list[str],
    counter_signals: list[str],
    *,
    evidence_title: str = "Evidence",
    counter_title: str = "Counter-signals",
) -> None:
    """Render evidence + counter-signals with neutral, product wording."""
    import streamlit as st

    if signals:
        st.markdown(f"**{evidence_title}**")
        for s in signals:
            st.markdown(f"- :green[+ {s}]")
    if counter_signals:
        st.markdown(f"**{counter_title}**")
        for s in counter_signals:
            st.markdown(f"- :red[− {s}]")


def technical_details(items: list[tuple[str, str]] | None, *, key: str | None = None) -> None:
    """Expose raw technical values behind an expander (never removed)."""
    import streamlit as st

    if not items:
        return
    with st.expander("Technical details", expanded=False):
        for label, value in items:
            st.markdown(f"`{label}` — {value}")


def metric_band(value: float, low: float = 35.0, medium: float = 65.0) -> str:
    """Map a 0..100 score onto LOW / MEDIUM / HIGH wording."""
    if value >= medium:
        return "HIGH"
    if value >= low:
        return "MEDIUM"
    return "LOW"


def review_annotation(
    review,
    *,
    weight: float | None = None,
    in_duplicate_group: bool = False,
    group_size: int | None = None,
    templated_score: float | None = None,
    specificity_score_value: float | None = None,
) -> list[str]:
    """Concise per-review attributes for scan-friendly review rows."""
    tags: list[str] = []
    if weight is not None:
        tags.append(f"weight {weight:.2f}")
    if in_duplicate_group:
        tags.append(f"in repeated-text group of {group_size or '?'}")
    if templated_score is not None and templated_score >= 65:
        tags.append("templated text")
    if specificity_score_value is not None:
        tags.append(f"specificity {specificity_score_value:.0f}")
    return tags
