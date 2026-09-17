#!/usr/bin/env python3
"""Performance benchmark for the ReviewScope pipeline (SPEC.md §8).

Measures, on an in-memory DuckDB store:

* the cold full-dataset embedding bootstrap;
* per-place ``AnalysisEngine.analyze`` over the demo corpus;
* a warm re-analysis of one place (embeddings already resident);
* a synthetic ~5k-review place to exercise analysis at a larger scale.

Usage::

    python scripts/benchmark.py
"""

from __future__ import annotations

import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewscope.analysis.engine import AnalysisEngine  # noqa: E402
from reviewscope.ingestion.csv_adapter import CSVAdapter  # noqa: E402
from reviewscope.models.review import NormalizedReview  # noqa: E402
from reviewscope.storage import DuckDBStore  # noqa: E402

DEMO_CSV = Path(__file__).resolve().parents[1] / "data" / "demo_reviews.csv"


def _seconds(label: str, fn):
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print(f"  {label:<52} {elapsed:8.2f}s")
    return result, elapsed


def _synthetic_place(n: int, seed: int = 7) -> list[NormalizedReview]:
    """One big place with ``n`` distinct organic-like reviews over ~2 years."""
    rng = random.Random(seed)
    days = (date(2026, 9, 1) - date(2024, 9, 1)).days
    bodies = [
        "Взял американо и чизкейк, десерт чуть сладковат, но кофе отличный.",
        "Стейк рибай принесли с кровью за 18 минут, гарнир не пересолен.",
        "Лечил зуб под микроскопом, врач показал каналы на снимке до и после.",
        "Тренируюсь по абонементу, зал чистый, есть кардио и свободные веса.",
        "Номер с видом на Волгу, завтрак шведский, кондиционер тихий.",
        "Покупал ноутбук, консультант сравнил три модели по матрице и весу.",
        "Куриный бульон остыл, но хлеб свежий, кофе подали за 12 минут.",
        "Пришёл в субботу, попросили чек для организации, вернулись через два дня.",
        "Понравился дизайн интерьера и вид на набережную, брал раф на овсяном.",
        "Очередь была, зато быстро решали вопросы, персонал вежливый.",
    ]
    out: list[NormalizedReview] = []
    for i in range(n):
        day = date(2024, 9, 1) + timedelta(days=rng.randint(0, days))
        out.append(
            NormalizedReview(
                review_id=f"s{i:05d}",
                place_id="big",
                place_name="Синтетический большой",
                place_category="coffee",
                reviewer_id=f"su{i % 800:04d}",
                reviewer_name=f"Гость {i % 800}",
                rating=rng.choices([5, 4, 3, 2, 1], weights=[45, 28, 14, 7, 6])[0],
                text=f"{rng.choice(bodies)} Личное примечание {i} — детали о визите.",
                published_at=day.isoformat(),
                city="Москва",
                region="Московская область",
                country="RU",
                latitude=55.75,
                longitude=37.61,
                source="bench",
            )
        )
    return out


def main() -> int:
    print(f"Demo dataset: {DEMO_CSV}")
    reviews = CSVAdapter().load(DEMO_CSV).reviews
    print(f"  {len(reviews)} reviews")

    store = DuckDBStore()
    store.ingest(reviews)
    engine = AnalysisEngine(store, use_embeddings=True)

    print("\nCold pipeline (fresh store + engine, embeds every review once):")
    cold_store = DuckDBStore()
    cold_store.ingest(reviews)
    cold_engine = AnalysisEngine(cold_store, use_embeddings=True)
    _seconds("bootstrap embeddings (all demo reviews)", cold_engine._all)
    _seconds("cold analyze 'p1'", lambda: cold_engine.analyze("p1"))
    cold_store.close()

    print("\nWarm suite over the shared engine:")
    places = store.list_places()["place_id"].tolist()
    total = 0.0
    for pid in places:
        _, t = _seconds(f"analyze {pid}", lambda pid=pid: engine.analyze(pid))
        total += t
    print(f"  {'warm suite total':<52} {total:8.2f}s")
    _seconds("warm re-analyze 'p1' (embeddings resident)", lambda: engine.analyze("p1"))

    print("\nSynthetic scale check (one place, ~5 000 reviews):")
    big_reviews = _synthetic_place(n=5000)
    big_store = DuckDBStore()
    big_store.ingest(big_reviews)
    big_engine = AnalysisEngine(big_store, use_embeddings=True)
    _seconds("bootstrap embeddings (5000 reviews)", big_engine._all)
    _seconds("analyze big place (5000 reviews)", lambda: big_engine.analyze("big"))
    big_store.close()

    store.close()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
