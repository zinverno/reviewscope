"""DuckDB storage for ReviewScope (SPEC.md §3, §32).

The store owns two tables:

* ``reviews`` — the normalized review records;
* ``embeddings_cache`` — persisted sentence embeddings keyed by
  ``(review_id, text_hash, model_name)`` so unchanged texts are never
  re-embedded on restart (SPEC.md §32).

The store is the single persistence boundary: ingestion writes reviews into
it, the analysis engine reads DataFrames out of it, and the embedding cache
lives in the same database file so a repeat run needs no re-computation.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from reviewscope.models.review import NormalizedReview

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id VARCHAR PRIMARY KEY,
    place_id VARCHAR,
    place_name VARCHAR,
    place_category VARCHAR,
    reviewer_id VARCHAR,
    reviewer_name VARCHAR,
    rating INTEGER,
    text VARCHAR,
    published_at VARCHAR,
    city VARCHAR,
    region VARCHAR,
    country VARCHAR,
    latitude DOUBLE,
    longitude DOUBLE,
    source VARCHAR,
    source_url VARCHAR
);

CREATE TABLE IF NOT EXISTS embeddings_cache (
    review_id VARCHAR,
    text_hash VARCHAR,
    model_name VARCHAR,
    embedding DOUBLE[],
    PRIMARY KEY (review_id, text_hash, model_name)
);
"""

_COLUMNS = (
    "review_id",
    "place_id",
    "place_name",
    "place_category",
    "reviewer_id",
    "reviewer_name",
    "rating",
    "text",
    "published_at",
    "city",
    "region",
    "country",
    "latitude",
    "longitude",
    "source",
    "source_url",
)


def _review_to_row(review: NormalizedReview) -> tuple:
    return (
        review.review_id,
        review.place_id,
        review.place_name,
        review.place_category,
        review.reviewer_id,
        review.reviewer_name,
        review.rating,
        review.text,
        review.published_at,
        review.city,
        review.region,
        review.country,
        review.latitude,
        review.longitude,
        review.source,
        review.source_url,
    )


def _row_to_review(row: dict) -> NormalizedReview:
    return NormalizedReview(
        review_id=str(row["review_id"]),
        place_id=str(row["place_id"]),
        place_name=row.get("place_name"),
        place_category=row.get("place_category"),
        reviewer_id=str(row["reviewer_id"]),
        reviewer_name=row.get("reviewer_name"),
        rating=None if row.get("rating") is None else int(row["rating"]),
        text=row.get("text"),
        published_at=row.get("published_at"),
        city=row.get("city"),
        region=row.get("region"),
        country=row.get("country"),
        latitude=row.get("latitude"),
        longitude=row.get("longitude"),
        source=row.get("source"),
        source_url=row.get("source_url"),
    )


class DuckDBStore:
    """A thin persistence facade over a DuckDB connection."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        read_only: bool = False,
        con: duckdb.DuckDBPyConnection | None = None,
    ) -> None:
        if con is not None:
            self._con = con
        else:
            try:
                self._con = duckdb.connect(str(db_path) if db_path else ":memory:")
            except duckdb.Error:
                raise
        self.read_only = read_only
        self._owns_connection = con is None
        if not read_only:
            self._con.execute(_SCHEMA_SQL)

    # -- connection lifecycle -------------------------------------------------

    def close(self) -> None:
        if self._owns_connection:
            self._con.close()

    def __enter__(self) -> DuckDBStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._con

    # -- reviews --------------------------------------------------------------

    def ingest(self, reviews: Iterable[NormalizedReview]) -> int:
        """Upsert reviews into the ``reviews`` table; returns rows written."""
        assert not self.read_only, "store opened read-only"
        rows = [_review_to_row(r) for r in reviews]
        if not rows:
            return 0
        self._con.executemany(
            f"INSERT OR REPLACE INTO reviews ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
            rows,
        )
        return len(rows)

    def review_count(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0])

    def distinct_reviewer_count(self) -> int:
        return int(
            self._con.execute("SELECT COUNT(DISTINCT reviewer_id) FROM reviews").fetchone()[
                0
            ]
        )

    def list_places(self) -> pd.DataFrame:
        """A summary DataFrame of distinct places with review counts."""
        return self._con.execute(
            """
            SELECT place_id, place_name, place_category, city, region, country,
                   ANY_VALUE(latitude) AS latitude,
                   ANY_VALUE(longitude) AS longitude,
                   COUNT(*)              AS review_count,
                   COUNT(DISTINCT reviewer_id) AS reviewer_count,
                   AVG(rating)           AS avg_rating,
                   MIN(published_at)     AS first_seen,
                   MAX(published_at)     AS last_seen
            FROM reviews
            GROUP BY place_id, place_name, place_category, city, region, country
            ORDER BY place_name
            """
        ).df()

    def place_counts(self, place_id: str | None = None) -> int:
        if place_id is None:
            return self.review_count()
        return int(
            self._con.execute(
                "SELECT COUNT(*) FROM reviews WHERE place_id = ?", [place_id]
            ).fetchone()[0]
        )

    def fetch_reviews(self, place_id: str | None = None) -> list[NormalizedReview]:
        """Return reviews as normalized model objects (for the analysis core)."""
        if place_id is None:
            df = self._con.execute("SELECT * FROM reviews").df()
        else:
            df = self._con.execute(
                "SELECT * FROM reviews WHERE place_id = ?", [place_id]
            ).df()
        return [_row_to_review(row) for row in df.to_dict(orient="records")]

    def reviews_frame(
        self,
        place_id: str | None = None,
        reviewer_id: str | None = None,
    ) -> pd.DataFrame:
        """Reviews as a DataFrame with a parsed ``published_at_dt`` column."""
        sql = "SELECT * FROM reviews"
        clauses: list[str] = []
        params: list[object] = []
        if place_id is not None:
            clauses.append("place_id = ?")
            params.append(place_id)
        if reviewer_id is not None:
            clauses.append("reviewer_id = ?")
            params.append(reviewer_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        df = self._con.execute(sql, params).df()
        df["published_at_dt"] = pd.to_datetime(df["published_at"], errors="coerce")
        return df

    def reviewers_for_place(self, place_id: str) -> list[str]:
        rows = self._con.execute(
            "SELECT DISTINCT reviewer_id FROM reviews WHERE place_id = ?", [place_id]
        ).fetchall()
        return [r[0] for r in rows]

    # -- embeddings cache (SPEC.md §32) ---------------------------------------

    @staticmethod
    def _to_vector(value) -> np.ndarray | None:
        if value is None:
            return None
        arr = np.asarray(value, dtype=np.float32)
        if arr.size == 0:
            return None
        return arr

    def get_cached_embeddings(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[tuple[str, str, str], np.ndarray]:
        """Return cached embeddings for ``(review_id, text_hash, model_name)`` keys."""
        result: dict[tuple[str, str, str], np.ndarray] = {}
        if not keys:
            return result
        clauses = " OR ".join(
            "(review_id = ? AND text_hash = ? AND model_name = ?)" for _ in keys
        )
        params: list[str] = [part for key in keys for part in key]
        rows = self._con.execute(
            "SELECT review_id, text_hash, model_name, embedding "
            f"FROM embeddings_cache WHERE {clauses}",
            params,
        ).fetchall()
        for review_id, text_hash, model_name, embedding in rows:
            vector = self._to_vector(embedding)
            if vector is not None:
                result[(review_id, text_hash, model_name)] = vector
        return result

    def store_cached_embeddings(self, rows: list[tuple[str, str, str, Iterable[float]]]) -> int:
        """Upsert embedding cache rows: ``(review_id, text_hash, model_name, embedding)``."""
        if not rows:
            return 0
        prepared = [(r[0], r[1], r[2], list(r[3])) for r in rows]
        self._con.executemany(
            "INSERT OR REPLACE INTO embeddings_cache "
            "(review_id, text_hash, model_name, embedding) VALUES (?, ?, ?, ?)",
            prepared,
        )
        return len(prepared)

    def cached_embedding_count(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) FROM embeddings_cache").fetchone()[0])

    # -- misc -----------------------------------------------------------------

    def table_exists(self, name: str) -> bool:
        row = self._con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [name]
        ).fetchone()
        return bool(row and row[0] > 0)
