"""Dataset-level aggregation over every place of the selected dataset.

Phase 16. This module is the (cached) analytical seam behind the **Discover**
page. It answers dataset-level questions without inventing anything new:

* it calls the production :class:`~reviewscope.analysis.engine.AnalysisEngine`
  once per place and aggregates what the place already produces;
* the only extra per-review number it computes is the production
  ``specificity_score``, because ``AnalyzedPlace`` does not expose it — the
  same orchestration pattern as ``reviewscope.validation.scoring``;
* metrics the dataset cannot support are ``None``, so the UI can render
  ``N/A`` instead of a misleading zero (missing evidence is not negative
  evidence);
* the per-place pass is expensive (~0.2 s/place on a 10k-review corpus), so it
  is memoized in process and mirrored to a small JSON sidecar keyed by the
  dataset identity (resolved path + size + mtime + embedding model).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from reviewscope.analysis.engine import AnalysisEngine, AnalyzedPlace
from reviewscope.analysis.specificity import specificity_score
from reviewscope.config import CONFIG
from reviewscope.storage import DuckDBStore

#: Rendered wherever the dataset cannot support a metric.
NOT_AVAILABLE = "N/A"

#: A repeated-text group enters the duplicate-rate share only at this size.
#: Identical to the Overview page definition (groups of 3+ reviews), so the
#: dataset-level rate and the place-level rate mean the same thing.
DUP_RATE_MIN_GROUP = 3

#: Bumped whenever the cached payload layout changes.
CACHE_VERSION = 4

#: One row per place, in display order. ``None`` means *unavailable*.
PLACE_COLUMNS: tuple[str, ...] = (
    "place_id",
    "place_name",
    "place_category",
    "review_count",
    "reviewer_count",
    "dated_review_count",
    "raw_rating",
    "weighted_rating",
    "rating_delta",
    "abs_rating_delta",
    "duplicate_rate",
    "duplicate_group_count",
    "largest_duplicate_group",
    "median_specificity",
    "mean_specificity",
    "topic_cluster_count",
    "templated_high_count",
    "templated_max",
    "templated_median",
    "one_star_share",
    "five_star_share",
    "coordinated_score",
    "coordinated_confidence",
    "analysis_error",
)

#: Count columns kept as nullable integers (never silently zero-filled).
_COUNT_COLUMNS = (
    "review_count",
    "reviewer_count",
    "dated_review_count",
    "duplicate_group_count",
    "largest_duplicate_group",
    "topic_cluster_count",
    "templated_high_count",
)

#: ``(column, label, kind)`` for the rendered comparison table. ``kind`` drives
#: formatting: ``text`` / ``int`` / ``float`` (2 decimals) / ``pct`` (1 decimal).
DISPLAY_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("place_name", "Place", "text"),
    ("place_category", "Category", "text"),
    ("review_count", "Reviews", "int"),
    ("reviewer_count", "Reviewers", "int"),
    ("raw_rating", "Raw rating", "float"),
    ("weighted_rating", "Weighted rating", "float"),
    ("abs_rating_delta", "Raw − weighted", "float"),
    ("duplicate_rate", "Duplicate rate", "pct"),
    # 2+ = every detected repeated-text group (pairs included), the same scope
    # as the Duplicates page; the duplicate rate above counts 3+ groups only.
    ("duplicate_group_count", "Dup groups (2+)", "int"),
    ("largest_duplicate_group", "Largest group", "int"),
    ("median_specificity", "Median specificity", "float"),
    ("mean_specificity", "Mean specificity", "float"),
    ("topic_cluster_count", "Topic clusters", "int"),
    ("templated_high_count", "Templated ≥65", "int"),
    ("templated_max", "Max templated", "float"),
    ("one_star_share", "1★ share", "pct"),
    ("five_star_share", "5★ share", "pct"),
    ("coordinated_score", "Coordinated", "float"),
)

#: Sort keys offered by the Discover page, mapped onto real columns.
SORT_OPTIONS: dict[str, str] = {
    "Reviews": "review_count",
    "Duplicate rate": "duplicate_rate",
    "Largest repeated group": "largest_duplicate_group",
    "Raw − weighted delta": "abs_rating_delta",
    "Median specificity": "median_specificity",
    "Topic clusters": "topic_cluster_count",
    "Max templated score": "templated_max",
    "1★ share": "one_star_share",
    "5★ share": "five_star_share",
    "Coordinated score": "coordinated_score",
    "Place name": "place_name",
    "Category": "place_category",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetCapabilities:
    """What the selected dataset can and cannot support.

    Derived from the raw review fields only (no detector runs), so it is
    available before the expensive per-place pass and stays valid even when
    that pass fails.
    """

    total_reviews: int = 0
    total_places: int = 0
    total_categories: int = 0
    dated_reviews: int = 0
    rated_reviews: int = 0
    text_reviews: int = 0
    located_reviews: int = 0
    distinct_reviewers: int = 0
    reviewers_with_multiple_places: int = 0

    @property
    def temporal_available(self) -> bool:
        """True when at least one review carries a usable publication date."""
        return self.dated_reviews > 0

    @property
    def reviewer_history_available(self) -> bool:
        """True when reviewers have history in more than one place.

        Reviewer-history analytics (geographic movement, cross-place
        comparison) need a reviewer that appears more than once; a corpus of
        one-off anonymous reviewers cannot support them.
        """
        return self.reviewers_with_multiple_places > 0

    @property
    def ratings_available(self) -> bool:
        return self.rated_reviews > 0

    @property
    def coordinated_available(self) -> bool:
        """Whether the coordinated-activity score is meaningful dataset-wide.

        The score's two heaviest components (volume burst, rating anomaly) are
        temporal. Without timestamps the production score degenerates to a
        structural subset — reported as unavailable rather than as a low
        observed value. The per-place score stays reachable from the place's
        own Overview page.
        """
        return self.temporal_available

    def note(self, unavailable: str, reason: str) -> str:
        """Render a capability note in the product's wording."""
        return f"{unavailable} unavailable: {reason}"

    def notes(self) -> list[str]:
        """Capability notes for the evidence this dataset cannot provide."""
        out: list[str] = []
        if not self.temporal_available:
            out.append(
                self.note(
                    "Temporal anomaly analysis",
                    "this dataset has no publication timestamps. "
                    "Burst, rating-anomaly and activity-window evidence is not "
                    "observable here, and is not counted as a negative signal.",
                )
            )
        if not self.reviewer_history_available:
            out.append(
                self.note(
                    "Reviewer-history analysis",
                    "no reviewer appears in more than one place in this dataset, "
                    "so there is no reviewer or geographic history to compare.",
                )
            )
        if self.located_reviews == 0:
            out.append(
                self.note(
                    "Review location mapping",
                    "this dataset has no review coordinates, so the "
                    "Reviewed Places map has nothing to draw.",
                )
            )
        if not self.ratings_available:
            out.append(
                self.note(
                    "Rating statistics",
                    "no review in this dataset carries a rating.",
                )
            )
        return out

    def to_dict(self) -> dict[str, int]:
        return {
            "total_reviews": self.total_reviews,
            "total_places": self.total_places,
            "total_categories": self.total_categories,
            "dated_reviews": self.dated_reviews,
            "rated_reviews": self.rated_reviews,
            "text_reviews": self.text_reviews,
            "located_reviews": self.located_reviews,
            "distinct_reviewers": self.distinct_reviewers,
            "reviewers_with_multiple_places": self.reviewers_with_multiple_places,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DatasetCapabilities:
        return cls(**{k: int(payload.get(k, 0) or 0) for k in cls().to_dict()})


@dataclass(frozen=True)
class DatasetSummary:
    """Cached dataset-level view: one row per place plus a category rollup."""

    places: pd.DataFrame
    categories: pd.DataFrame
    capabilities: DatasetCapabilities
    build_seconds: float = 0.0
    source: str = "live"
    built_at: str = ""
    errors: tuple[tuple[str, str], ...] = ()

    @property
    def places_analyzed(self) -> int:
        return int((self.places["analysis_error"].isna()).sum()) if len(self.places) else 0

    @property
    def places_failed(self) -> int:
        return len(self.errors)

    @property
    def places_with_duplicate_groups(self) -> int:
        return _count_where(self.places, "duplicate_group_count", lambda s: s > 0)

    @property
    def places_with_topic_clusters(self) -> int:
        return _count_where(self.places, "topic_cluster_count", lambda s: s > 0)

    @property
    def places_with_templated_high(self) -> int:
        return _count_where(self.places, "templated_high_count", lambda s: s > 0)


@dataclass(frozen=True)
class RankingSection:
    """One descriptive ranking shown on the Discover page."""

    key: str
    title: str
    description: str
    frame: pd.DataFrame
    available: bool = True
    note: str = ""


def _count_where(frame: pd.DataFrame, column: str, predicate: Callable[[pd.Series], pd.Series]) -> int:
    """Count places matching ``predicate``; missing values count as zero."""
    if frame.empty or column not in frame.columns:
        return 0
    values = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype(float)
    return int(predicate(values).sum())


# ---------------------------------------------------------------------------
# Per-place aggregation
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None and not (isinstance(v, float) and np.isnan(v))]
    if not clean:
        return None
    return float(np.median(clean))


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None and not (isinstance(v, float) and np.isnan(v))]
    if not clean:
        return None
    return float(np.mean(clean))


def _place_row(
    meta: dict[str, Any],
    analyzed: AnalyzedPlace,
    *,
    coordinated_available: bool,
) -> dict[str, Any]:
    """Aggregate one analysed place into a single comparison row."""
    reviews = analyzed.reviews
    count = len(reviews)
    row: dict[str, Any] = {
        "place_id": meta.get("place_id"),
        "place_name": meta.get("place_name") or meta.get("place_id"),
        "place_category": meta.get("place_category") or None,
        "review_count": count,
        "reviewer_count": len({r.reviewer_id for r in reviews}) if reviews else 0,
        "dated_review_count": sum(1 for r in reviews if r.published_at) if reviews else 0,
        "analysis_error": None,
    }

    # --- ratings (raw vs weighted) -----------------------------------------
    rated = [r.rating for r in reviews if r.rating is not None]
    if rated:
        raw = float(analyzed.raw_rating)
        weighted = float(analyzed.weighted_rating_value)
        # Same derivation as the Overview verdict line: the difference of the
        # two ratings that are displayed. The production ScoreResult also
        # carries a finer-grained ``details["delta"]`` computed *before*
        # production rounds raw/weighted; that value stays where production
        # shows it (the place Overview's technical details). Deriving it here
        # instead would make a row's delta differ from the two ratings printed
        # in the same row.
        delta = raw - weighted
        row["raw_rating"] = round(raw, 4)
        row["weighted_rating"] = round(weighted, 4)
        row["rating_delta"] = round(delta, 4)
        row["abs_rating_delta"] = round(abs(delta), 4)
        ones = sum(1 for r in rated if r == 1) / len(rated) * 100
        fives = sum(1 for r in rated if r == 5) / len(rated) * 100
        row["one_star_share"] = round(ones, 4)
        row["five_star_share"] = round(fives, 4)
    else:
        row["raw_rating"] = None
        row["weighted_rating"] = None
        row["rating_delta"] = None
        row["abs_rating_delta"] = None
        row["one_star_share"] = None
        row["five_star_share"] = None

    # --- repeated text -------------------------------------------------------
    groups = [g for g in analyzed.duplicate_groups if len(g.review_ids) >= 2]
    repeated_ids = {
        rid
        for g in analyzed.duplicate_groups
        if len(g.review_ids) >= DUP_RATE_MIN_GROUP
        for rid in g.review_ids
    }
    row["duplicate_group_count"] = len(groups)
    row["largest_duplicate_group"] = max((len(g.review_ids) for g in groups), default=0)
    row["duplicate_rate"] = (
        round(len(repeated_ids) / count * 100, 4) if count else None
    )

    # --- specificity (production scorer) -------------------------------------
    specs = [specificity_score(r.text_or_empty()).value for r in reviews] if reviews else []
    row["median_specificity"] = _median(specs)
    row["mean_specificity"] = _mean(specs)

    # --- topics / templated text --------------------------------------------
    row["topic_cluster_count"] = len(analyzed.clusters)
    tpl = [float(s.value) for s in analyzed.templated_scores]
    threshold = CONFIG.templated.high_threshold
    row["templated_high_count"] = sum(1 for v in tpl if v >= threshold)
    row["templated_max"] = round(max(tpl), 4) if tpl else None
    row["templated_median"] = _median(tpl)

    # --- coordinated activity: only where the evidence exists ----------------
    if coordinated_available and analyzed.coordinated is not None:
        row["coordinated_score"] = round(float(analyzed.coordinated.value), 4)
        row["coordinated_confidence"] = str(analyzed.coordinated.confidence)
    else:
        row["coordinated_score"] = None
        row["coordinated_confidence"] = None
    return row


def _failed_row(meta: dict[str, Any], error: str) -> dict[str, Any]:
    """Row for a place whose analysis failed: counts known, evidence ``None``."""
    row: dict[str, Any] = {col: None for col in PLACE_COLUMNS}
    row["place_id"] = meta.get("place_id")
    row["place_name"] = meta.get("place_name") or meta.get("place_id")
    row["place_category"] = meta.get("place_category") or None
    row["review_count"] = int(meta.get("review_count") or 0)
    row["analysis_error"] = error
    return row


def _capabilities(store: DuckDBStore) -> DatasetCapabilities:
    """Read the dataset's own fields to decide what evidence exists at all."""
    frame = store.reviews_frame()
    if frame.empty:
        return DatasetCapabilities()
    dated = frame["published_at_dt"].notna() if "published_at_dt" in frame else pd.Series(False, index=frame.index)
    text = frame["text"].fillna("").astype(str).str.strip().ne("") if "text" in frame else pd.Series(False, index=frame.index)
    lat = pd.to_numeric(frame.get("latitude"), errors="coerce") if "latitude" in frame else pd.Series(dtype=float)
    lon = pd.to_numeric(frame.get("longitude"), errors="coerce") if "longitude" in frame else pd.Series(dtype=float)
    located = lat.notna() & lon.notna()

    multi_place = 0
    if "reviewer_id" in frame:
        per_reviewer = frame.groupby("reviewer_id", dropna=True)["place_id"].nunique()
        multi_place = int((per_reviewer > 1).sum())

    categories = frame["place_category"] if "place_category" in frame else pd.Series(dtype=object)
    category_values = categories.fillna("").astype(str).str.strip()
    return DatasetCapabilities(
        total_reviews=int(len(frame)),
        total_places=int(frame["place_id"].nunique()) if "place_id" in frame else 0,
        total_categories=int(category_values[category_values.ne("")].nunique()),
        dated_reviews=int(dated.sum()),
        rated_reviews=int(frame["rating"].notna().sum()) if "rating" in frame else 0,
        text_reviews=int(text.sum()),
        located_reviews=int(located.sum()),
        distinct_reviewers=int(frame["reviewer_id"].nunique()) if "reviewer_id" in frame else 0,
        reviewers_with_multiple_places=multi_place,
    )


def _place_meta(store: DuckDBStore) -> pd.DataFrame:
    """One deterministic metadata row per place id.

    ``list_places`` groups by the full place metadata tuple, so a place id can
    in principle appear more than once. The row with the most reviews wins
    (most complete metadata), with name/id as deterministic tie-breakers.
    """
    meta = store.list_places()
    if meta.empty:
        return meta
    meta = meta.sort_values(
        ["place_id", "review_count", "place_name"],
        ascending=[True, False, True],
        kind="stable",
    )
    return meta.drop_duplicates(subset="place_id", keep="first").reset_index(drop=True)


def build_dataset_summary(
    store: DuckDBStore,
    engine: AnalysisEngine,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> DatasetSummary:
    """Analyse every place once and aggregate the dataset-level view.

    ``progress(done, total)`` is called after each place so a UI can show a
    progress bar over a pass that takes tens of seconds on a real corpus.
    A place whose analysis raises is kept as a row with ``analysis_error`` set
    and every evidence column ``None`` — a failure is never reported as a zero.
    """
    started = time.perf_counter()
    capabilities = _capabilities(store)
    meta = _place_meta(store)
    rows: list[dict[str, Any]] = []
    errors: list[tuple[str, str]] = []
    total = len(meta)
    step = max(1, total // 100)
    for i, record in enumerate(meta.to_dict(orient="records"), start=1):
        place_id = str(record["place_id"])
        try:
            analyzed = engine.analyze(place_id)
        except Exception as exc:  # noqa: BLE001 - one bad place must not kill the page
            message = f"{type(exc).__name__}: {exc}"
            errors.append((place_id, message))
            rows.append(_failed_row(record, message))
        else:
            rows.append(
                _place_row(
                    record,
                    analyzed,
                    coordinated_available=capabilities.coordinated_available,
                )
            )
        if progress is not None and (i % step == 0 or i == total):
            progress(i, total)

    places = _finalize_places_frame(pd.DataFrame(rows, columns=list(PLACE_COLUMNS)))
    categories = category_summary(places)
    return DatasetSummary(
        places=places,
        categories=categories,
        capabilities=capabilities,
        build_seconds=round(time.perf_counter() - started, 3),
        source="live",
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        errors=tuple(errors),
    )


def _finalize_places_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Stable dtypes: nullable ints for counts, floats for metrics, no reindex."""
    out = frame.copy()
    for column in PLACE_COLUMNS:
        if column not in out.columns:
            out[column] = pd.NA
    out = out[list(PLACE_COLUMNS)]
    for column in _COUNT_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    names = out["place_name"].astype(object)
    out["place_name"] = [
        str(name) if name is not None and not pd.isna(name) else str(pid)
        for name, pid in zip(names, out["place_id"], strict=True)
    ]
    out["place_category"] = out["place_category"].astype(object)
    out["analysis_error"] = out["analysis_error"].astype(object)
    out["coordinated_confidence"] = out["coordinated_confidence"].astype(object)
    out = out.sort_values(["place_name", "place_id"], kind="stable").reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Filtering / sorting
# ---------------------------------------------------------------------------


def filter_places(
    places: pd.DataFrame,
    *,
    categories: tuple[str, ...] | list[str] = (),
    min_reviews: int = 0,
    query: str = "",
    duplicate_rate_range: tuple[float, float] | None = None,
    sort_by: str = "review_count",
    descending: bool = True,
) -> pd.DataFrame:
    """Apply the Discover page filters and a deterministic sort.

    Filtering is a hard filter (rows disappear); sorting always breaks ties on
    ``place_name`` so repeated runs are stable.
    """
    if places.empty:
        return places.copy()

    out = places
    if categories:
        out = out[out["place_category"].astype(object).isin(list(categories))]
    if min_reviews:
        out = out[out["review_count"].fillna(0).astype(float) >= float(min_reviews)]
    if query:
        needle = query.strip().lower()
        if needle:
            names = out["place_name"].astype(str).str.lower()
            ids = out["place_id"].astype(str).str.lower()
            out = out[names.str.contains(needle, regex=False) | ids.str.contains(needle, regex=False)]
    if duplicate_rate_range is not None:
        low, high = duplicate_rate_range
        rates = pd.to_numeric(out["duplicate_rate"], errors="coerce")
        out = out[rates.isna() | ((rates >= float(low)) & (rates <= float(high)))]

    column = SORT_OPTIONS.get(sort_by, sort_by)
    if column in out.columns:
        out = out.sort_values(
            by=[column, "place_name"],
            ascending=[not descending, True],
            kind="stable",
            na_position="last",
        )
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _format(value: Any, kind: str) -> str:
    if value is None:
        return NOT_AVAILABLE
    try:
        if bool(pd.isna(value)):
            return NOT_AVAILABLE
    except (TypeError, ValueError):
        pass
    if kind == "int":
        return f"{int(value)}"
    if kind == "float":
        return f"{float(value):.2f}"
    if kind == "pct":
        return f"{float(value):.1f}%"
    text = str(value).strip()
    return text if text else NOT_AVAILABLE


def display_places_frame(places: pd.DataFrame) -> pd.DataFrame:
    """Formatted, human-readable view of :attr:`DatasetSummary.places`.

    Every unavailable value becomes ``N/A`` — the table must never imply an
    observed zero where the dataset simply carries no evidence.
    """
    data = {}
    for column, label, kind in DISPLAY_COLUMNS:
        source = places[column] if column in places.columns else pd.Series([None] * len(places))
        data[label] = [_format(v, kind) for v in source]
    return pd.DataFrame(data)


def _format_category_frame(categories: pd.DataFrame) -> pd.DataFrame:
    data: dict[str, list[str]] = {}
    for column in categories.columns:
        kind = _CATEGORY_KINDS.get(column, "float")
        data[CATEGORY_LABELS.get(column, column)] = [_format(v, kind) for v in categories[column]]
    return pd.DataFrame(data)


CATEGORY_LABELS: dict[str, str] = {
    "place_category": "Category",
    "places": "Places",
    "reviews": "Reviews",
    "median_reviews": "Median reviews",
    "median_raw_rating": "Median raw rating",
    "q1_raw_rating": "Raw rating Q1",
    "q3_raw_rating": "Raw rating Q3",
    "median_duplicate_rate": "Median duplicate rate",
    "q1_duplicate_rate": "Duplicate rate Q1",
    "q3_duplicate_rate": "Duplicate rate Q3",
    "median_specificity": "Median specificity",
    "q1_specificity": "Specificity Q1",
    "q3_specificity": "Specificity Q3",
    "topic_clusters_total": "Topic clusters",
    "places_with_clusters": "Places with clusters",
    "median_clusters": "Median clusters",
    "median_templated_max": "Median max templated",
}

CATEGORY_COLUMNS: tuple[str, ...] = tuple(CATEGORY_LABELS)

#: Render format per category column (``text`` for the label, counts as ints).
_CATEGORY_KINDS: dict[str, str] = {
    "place_category": "text",
    "places": "int",
    "reviews": "int",
    "median_reviews": "int",
    "topic_clusters_total": "int",
    "places_with_clusters": "int",
    "median_clusters": "int",
}


# ---------------------------------------------------------------------------
# Category rollup
# ---------------------------------------------------------------------------


def _safe_median(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return round(float(values.median()), 4)


def _safe_quantile(series: pd.Series, q: float) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return round(float(values.quantile(q)), 4)


def category_summary(places: pd.DataFrame) -> pd.DataFrame:
    """Per-category rollup with medians and quartiles (never a new score).

    Robust descriptive statistics only: medians and IQR-style quartiles, so a
    single large place cannot dominate a category. No suspicion score is
    produced — the category table is context for the place rankings.
    """
    if places.empty:
        return pd.DataFrame(columns=list(CATEGORY_COLUMNS))

    frame = places.copy()
    frame["place_category"] = frame["place_category"].fillna("(no category)").astype(str)
    rows = []
    for category, sub in frame.groupby("place_category", sort=True):
        clusters = pd.to_numeric(sub["topic_cluster_count"], errors="coerce")
        rows.append(
            {
                "place_category": category,
                "places": int(len(sub)),
                "reviews": int(pd.to_numeric(sub["review_count"], errors="coerce").fillna(0).sum()),
                "median_reviews": _safe_median(sub["review_count"]),
                "median_raw_rating": _safe_median(sub["raw_rating"]),
                "q1_raw_rating": _safe_quantile(sub["raw_rating"], 0.25),
                "q3_raw_rating": _safe_quantile(sub["raw_rating"], 0.75),
                "median_duplicate_rate": _safe_median(sub["duplicate_rate"]),
                "q1_duplicate_rate": _safe_quantile(sub["duplicate_rate"], 0.25),
                "q3_duplicate_rate": _safe_quantile(sub["duplicate_rate"], 0.75),
                "median_specificity": _safe_median(sub["median_specificity"]),
                "q1_specificity": _safe_quantile(sub["median_specificity"], 0.25),
                "q3_specificity": _safe_quantile(sub["median_specificity"], 0.75),
                "topic_clusters_total": int(clusters.fillna(0).sum()),
                "places_with_clusters": int((clusters.fillna(0) > 0).sum()),
                "median_clusters": _safe_median(sub["topic_cluster_count"]),
                "median_templated_max": _safe_median(sub["templated_max"]),
            }
        )
    result = pd.DataFrame(rows, columns=list(CATEGORY_COLUMNS))
    return result.sort_values(
        by=["reviews", "place_category"], ascending=[False, True], kind="stable"
    ).reset_index(drop=True)


def display_category_frame(categories: pd.DataFrame) -> pd.DataFrame:
    """Formatted category rollup for rendering."""
    if categories.empty:
        return pd.DataFrame(columns=list(CATEGORY_LABELS.values()))
    return _format_category_frame(categories)


# ---------------------------------------------------------------------------
# Descriptive rankings
# ---------------------------------------------------------------------------


def _ranked(
    places: pd.DataFrame,
    column: str,
    columns: list[tuple[str, str, str]],
    *,
    ascending: bool = False,
    top_n: int = 5,
) -> pd.DataFrame:
    subset = places[places[column].notna()]
    subset = subset.sort_values(
        by=[column, "place_name"],
        ascending=[ascending, True],
        kind="stable",
        na_position="last",
    ).head(top_n)
    data: dict[str, list[str]] = {}
    for col, label, kind in columns:
        data[label] = [_format(v, kind) for v in subset[col]]
    return pd.DataFrame(data)


def ranking_sections(
    places: pd.DataFrame,
    *,
    top_n: int = 5,
    coordinated_available: bool = False,
) -> list[RankingSection]:
    """Descriptive rankings shown on Discover.

    Wording is strictly descriptive ("highest duplicate rate", "most topic
    clusters"). Ranking position is never a verdict: the same place can top a
    "most 5★ reviews" list and a "lowest specificity" list.
    """
    if places.empty:
        return []

    top = partial(_ranked, top_n=top_n)
    name_cat = [("place_name", "Place", "text"), ("place_category", "Category", "text")]
    sections: list[RankingSection] = [
        RankingSection(
            key="duplicate_rate",
            title="Highest duplicate rate",
            description="Share of the place's reviews that sit in a repeated-text group of "
            f"{DUP_RATE_MIN_GROUP}+ reviews.",
            frame=top(
                places,
                "duplicate_rate",
                [*name_cat, ("review_count", "Reviews", "int"), ("duplicate_rate", "Duplicate rate", "pct"),
                 ("largest_duplicate_group", "Largest group", "int")],
            ),
            available=bool(places["duplicate_rate"].notna().any()),
            note="No duplicate rate could be measured in this dataset, so there is nothing to rank.",
        ),
        RankingSection(
            key="largest_group",
            title="Largest repeated review groups",
            description=(
                "Biggest single repeated-text group per place (identical or near-identical text). "
                f"`Dup groups (2+)` counts every detected group, pairs included - the same scope as "
                f"the Duplicates page; the duplicate rate above counts groups of {DUP_RATE_MIN_GROUP}+."
            ),
            frame=top(
                places,
                "largest_duplicate_group",
                [*name_cat, ("largest_duplicate_group", "Largest group", "int"),
                 ("duplicate_group_count", "Dup groups (2+)", "int"), ("review_count", "Reviews", "int")],
            ),
            available=bool((places["largest_duplicate_group"].fillna(0) > 0).any()),
            note="No repeated-text groups were detected anywhere in this dataset.",
        ),
        RankingSection(
            key="rating_delta",
            title="Largest raw-vs-weighted rating delta",
            description="Biggest absolute gap between the raw average rating and the weighted rating.",
            frame=top(
                places,
                "abs_rating_delta",
                [*name_cat, ("review_count", "Reviews", "int"), ("raw_rating", "Raw rating", "float"),
                 ("weighted_rating", "Weighted rating", "float"), ("abs_rating_delta", "Raw − weighted", "float")],
            ),
            available=bool(places["abs_rating_delta"].notna().any()),
            note="No rated reviews in this dataset, so there is no rating delta to rank.",
        ),
        RankingSection(
            key="high_specificity",
            title="Highest specificity",
            description="Places whose reviews carry the most concrete detail (production specificity score).",
            frame=top(
                places,
                "median_specificity",
                [*name_cat, ("review_count", "Reviews", "int"), ("median_specificity", "Median specificity", "float"),
                 ("mean_specificity", "Mean specificity", "float")],
            ),
            available=bool(places["median_specificity"].notna().any()),
            note="No specificity score could be computed in this dataset, so there is nothing to rank.",
        ),
        RankingSection(
            key="low_specificity",
            title="Lowest specificity",
            description="Places whose reviews are the most generic. Generic text is common in real corpora — "
            "it is a description, not a verdict.",
            frame=top(
                places,
                "median_specificity",
                [*name_cat, ("review_count", "Reviews", "int"), ("median_specificity", "Median specificity", "float")],
                ascending=True,
            ),
            available=bool(places["median_specificity"].notna().any()),
            note="No specificity score could be computed in this dataset, so there is nothing to rank.",
        ),
        RankingSection(
            key="topic_clusters",
            title="Most semantic topic clusters",
            description="Number of semantic topic groups the clustering found among the place's reviews.",
            frame=top(
                places,
                "topic_cluster_count",
                [*name_cat, ("review_count", "Reviews", "int"), ("topic_cluster_count", "Topic clusters", "int")],
            ),
            available=bool((places["topic_cluster_count"].fillna(0) > 0).any()),
            note="No semantic topic clusters were formed in this dataset.",
        ),
        RankingSection(
            key="templated",
            title="Highest templated-text scores",
            description=f"Strongest participation in a reusable template family inside the same place "
            f"(templated score ≥ {CONFIG.templated.high_threshold:g} is labelled templated in review rows).",
            frame=top(
                places,
                "templated_max",
                [*name_cat, ("review_count", "Reviews", "int"), ("templated_max", "Max templated", "float"),
                 ("templated_median", "Median templated", "float"),
                 ("templated_high_count", f"≥{CONFIG.templated.high_threshold:g}", "int")],
            ),
            available=bool(places["templated_max"].notna().any()),
            note="No templated-text score could be computed in this dataset, so there is nothing to rank.",
        ),
        RankingSection(
            key="cohort",
            title="Largest review cohorts",
            description="Places with the most reviews in the dataset — the biggest evidence pool to work with.",
            frame=top(
                places,
                "review_count",
                [*name_cat, ("review_count", "Reviews", "int"), ("reviewer_count", "Reviewers", "int")],
            ),
            available=bool(places["review_count"].notna().any()),
            note="",
        ),
        RankingSection(
            key="one_star",
            title="Most 1★ reviews",
            description="Highest share of one-star reviews. Low ratings are a normal outcome, not evidence of anything.",
            frame=top(
                places,
                "one_star_share",
                [*name_cat, ("review_count", "Reviews", "int"), ("one_star_share", "1★ share", "pct"),
                 ("raw_rating", "Raw rating", "float")],
            ),
            available=bool(places["one_star_share"].notna().any()),
            note="No rated reviews in this dataset, so there is nothing to rank.",
        ),
        RankingSection(
            key="five_star",
            title="Most 5★ reviews",
            description="Highest share of five-star reviews — the counterpart extreme.",
            frame=top(
                places,
                "five_star_share",
                [*name_cat, ("review_count", "Reviews", "int"), ("five_star_share", "5★ share", "pct"),
                 ("raw_rating", "Raw rating", "float")],
            ),
            available=bool(places["five_star_share"].notna().any()),
            note="No rated reviews in this dataset, so there is nothing to rank.",
        ),
    ]

    sections.append(
        RankingSection(
            key="coordinated",
            title="Highest coordinated activity score",
            description="Production coordinated-activity score, ranked across places.",
            frame=top(
                places,
                "coordinated_score",
                [*name_cat, ("review_count", "Reviews", "int"), ("coordinated_score", "Coordinated", "float"),
                 ("coordinated_confidence", "Confidence", "text")],
            ),
            available=coordinated_available and bool(places["coordinated_score"].notna().any()),
            note=(
                "Coordinated activity is not ranked here: the score's strongest components are "
                "temporal, and this dataset has no publication timestamps. It is still computed "
                "per place — open a place's Overview to read it with its evidence."
            ),
        )
    )

    # An unavailable section must not be able to render a table: the UI shows
    # the note instead, and the frame is emptied here so no caller can show a
    # table of values that would read as a ranking.
    return [
        section
        if section.available
        else replace(section, frame=section.frame.iloc[0:0].copy())
        for section in sections
    ]


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_MEMORY: dict[tuple, DatasetSummary] = {}
_COUNTERS = {"live_builds": 0, "memory_hits": 0, "disk_hits": 0, "disk_writes": 0}


def dataset_identity(db_path: str, store: DuckDBStore | None = None) -> tuple:
    """A stable cache key for the selected dataset.

    Uses the resolved path plus size and modification time of the DuckDB file,
    which changes whenever the dataset is rebuilt or re-ingested, plus the
    embedding model (topic clustering and semantic duplicate groups depend on
    it). Non-file stores (in-memory DuckDB) fall back to connection identity.
    """
    model = CONFIG.embedding.model_name
    try:
        path = Path(db_path).expanduser().resolve()
        stat = path.stat()
        return (str(path), stat.st_size, stat.st_mtime_ns, model)
    except OSError:
        connection_id = id(store.connection()) if store is not None else 0
        return (str(db_path), -1, -1, model, connection_id)


def _cache_dir() -> Path:
    override = os.environ.get("RS_CACHE_DIR")
    base = Path(override) if override else Path(
        os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    )
    return base / "reviewscope"


def _disk_path(identity: tuple) -> Path:
    digest = hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:20]
    return _cache_dir() / f"discovery_v{CACHE_VERSION}_{digest}.json"


def _enabled() -> bool:
    return os.environ.get("RS_DISCOVERY_DISK_CACHE", "1").strip().lower() not in {"0", "false", "off", "no"}


def _json_safe(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    out = frame.copy()
    for column in out.columns:
        if str(out[column].dtype) == "Int64":
            out[column] = out[column].astype(object)
    return [
        {k: (None if (v is None or v is pd.NA or (isinstance(v, float) and np.isnan(v))) else v) for k, v in row.items()}
        for row in out.to_dict(orient="records")
    ]


def _write_disk(identity: tuple, summary: DatasetSummary) -> None:
    if not _enabled():
        return
    try:
        path = _disk_path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_version": CACHE_VERSION,
            "identity": list(identity),
            "build_seconds": summary.build_seconds,
            "built_at": summary.built_at,
            "errors": [list(pair) for pair in summary.errors],
            "capabilities": summary.capabilities.to_dict(),
            "places": _json_safe(summary.places),
            "categories": _json_safe(summary.categories),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)
        with _LOCK:
            _COUNTERS["disk_writes"] += 1
    except OSError:
        return


def _read_disk(identity: tuple) -> DatasetSummary | None:
    if not _enabled():
        return None
    try:
        path = _disk_path(identity)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("cache_version") != CACHE_VERSION or payload.get("identity") != list(identity):
        return None
    places = _finalize_places_frame(pd.DataFrame(payload.get("places") or [], columns=list(PLACE_COLUMNS)))
    categories = pd.DataFrame(payload.get("categories") or [], columns=list(CATEGORY_COLUMNS))
    return DatasetSummary(
        places=places,
        categories=categories,
        capabilities=DatasetCapabilities.from_dict(payload.get("capabilities") or {}),
        build_seconds=float(payload.get("build_seconds") or 0.0),
        source="disk",
        built_at=str(payload.get("built_at") or ""),
        errors=tuple(tuple(pair) for pair in payload.get("errors") or []),
    )


def get_dataset_summary(
    db_path: str,
    store: DuckDBStore,
    engine: AnalysisEngine,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> DatasetSummary:
    """Cached :func:`build_dataset_summary` keyed by dataset identity.

    Order of resolution: in-process cache → on-disk sidecar → live build.
    Embeddings are never recomputed: the engine reuses the persisted
    ``embeddings_cache`` rows, and a dataset with them fully populated encodes
    nothing at all.
    """
    identity = dataset_identity(db_path, store)
    key = (CACHE_VERSION, identity)
    with _LOCK:
        cached = _MEMORY.get(key)
    if cached is not None:
        with _LOCK:
            _COUNTERS["memory_hits"] += 1
        return DatasetSummary(
            places=cached.places,
            categories=cached.categories,
            capabilities=cached.capabilities,
            build_seconds=cached.build_seconds,
            source="memory",
            built_at=cached.built_at,
            errors=cached.errors,
        )

    restored = _read_disk(identity)
    if restored is not None:
        with _LOCK:
            _MEMORY[key] = restored
            _COUNTERS["disk_hits"] += 1
        return restored

    summary = build_dataset_summary(store, engine, progress=progress)
    with _LOCK:
        _MEMORY[key] = summary
        _COUNTERS["live_builds"] += 1
    _write_disk(identity, summary)
    return summary


def clear_dataset_cache(*, disk: bool = False) -> None:
    """Drop the in-process summary cache (optionally the sidecars too)."""
    with _LOCK:
        _MEMORY.clear()
        for key in _COUNTERS:
            _COUNTERS[key] = 0
    if disk and _enabled():
        try:
            for path in _cache_dir().glob(f"discovery_v{CACHE_VERSION}_*.json"):
                path.unlink()
        except OSError:
            return


def dataset_cache_info() -> dict[str, int]:
    """Cache counters for the performance caption / tests."""
    with _LOCK:
        return {"entries": len(_MEMORY), **_COUNTERS}


def source_badge(source: str) -> str:
    """Human wording for where the rendered summary came from."""
    return {
        "live": "built now (cold)",
        "memory": "reused in-process cache (warm)",
        "disk": "reused on-disk cache (warm)",
    }.get(source, source)
