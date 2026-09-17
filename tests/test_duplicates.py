"""Unit tests for duplicate detection (SPEC.md §11, §37)."""

from __future__ import annotations

import numpy as np

from reviewscope.analysis.duplicates import (
    DuplicateDetector,
    _normalize_text,
    _rapid_score,
)
from reviewscope.models.review import NormalizedReview


def _review(review_id: str, text: str, place_id: str = "p1") -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id, place_id=place_id, reviewer_id=f"u{review_id}", text=text
    )


class TestExactDuplicates:
    def test_identical_texts_grouped(self) -> None:
        r1 = _review("a", "Отличное место, обслуживание на высоте")
        r2 = _review("b", "Отличное место, обслуживание на высоте")
        r3 = _review("c", "Совсем другой текст про погоду")
        groups = DuplicateDetector().detect([r1, r2, r3])
        assert any(g.exact_count >= 1 and len(g.review_ids) == 2 for g in groups)

    def test_different_texts_no_group(self) -> None:
        reviews = [
            _review("r0", "Уникальный текст про кофе и десерты"),
            _review("r1", "Вчера смотрели фильм про космос"),
            _review("r2", "Купил новый ноутбук и мышь"),
            _review("r3", "Мой кот любит спать на клавиатуре"),
            _review("r4", "Осенью в парке жёлтые листья"),
            _review("r5", "Футбольный матч закончился вничью"),
        ]
        groups = DuplicateDetector().detect(reviews)
        assert groups == []

    def test_empty_input(self) -> None:
        assert DuplicateDetector().detect([]) == []

    def test_single_review(self) -> None:
        assert DuplicateDetector().detect([_review("a", "привет")]) == []


class TestFuzzyDuplicates:
    def test_near_identical_texts_detected(self) -> None:
        base = "Хорошая стоматология, лечение было без боли, врач всё рассказал по снимкам. Цена нормальная."
        # Few-words-moved edit variant (char-level ratio ~0.92), a genuine fuzzy/near duplicate.
        slightly_different = "Хорошая стоматология, лечение прошло без боли, врач рассказал всё по снимкам. Цена нормальная."
        r1 = _review("a", base)
        r2 = _review("b", slightly_different)
        r3 = _review("c", "Совершенно непохожий текст про другое место в другом городе")
        groups = DuplicateDetector().detect([r1, r2, r3])
        assert any(len(g.review_ids) == 2 for g in groups)


class TestSemanticDuplicates:
    def test_paraphrased_reviews_require_embeddings(self) -> None:
        """Paraphrases (~0.78 char-level) are NOT fuzzy duplicates — they only
        group with embeddings, matching SPEC §11 semantic level."""
        base = "Хорошая стоматология, лечение было без боли, врач всё рассказал по снимкам. Цена нормальная."
        paraphrase = "Хорошая стоматология, лечили без боли, врач показывал снимки и всё объяснял. Цена нормальная."
        assert _rapid_score(_normalize_text(base), _normalize_text(paraphrase)) < 0.85
        r1 = _review("a", base)
        r2 = _review("b", paraphrase)
        groups = DuplicateDetector().detect([r1, r2])
        assert groups == []

    def test_cosine_similar_embeddings_grouped(self) -> None:
        rng = np.random.RandomState(42)
        base = rng.rand(384).astype(np.float32)
        r1 = _review("a", "Текст один")
        r2 = _review("b", "Текст другой")
        r3 = _review("c", "Текст третий")
        embeddings = np.array([base, base + 0.001, rng.rand(384)])
        groups = DuplicateDetector().detect([r1, r2, r3], embeddings=embeddings)
        assert any(g.semantic_count > 0 for g in groups)


class TestMixedGroups:
    def test_exact_plus_semantic_merge(self) -> None:
        r1 = _review("a", "Кофе отличный, персонал приветливый")
        r2 = _review("b", "Кофе отличный, персонал приветливый")  # exact dup of r1
        r3 = _review("c", "Кофе хороший, персонал дружелюбный")  # near-dup of r1/r2
        rng = np.random.RandomState(7)
        base = rng.rand(384).astype(np.float32)
        embeddings = np.array([base, base, base + 0.05])
        groups = DuplicateDetector().detect([r1, r2, r3], embeddings=embeddings)
        assert any(len(g.review_ids) >= 3 for g in groups)


class TestNormalizeText:
    def test_lowercases_and_trims(self) -> None:
        assert _normalize_text("  Привет   Мир  ") == "привет мир"

    def test_none_returns_empty(self) -> None:
        assert _normalize_text(None) == ""


class TestDemoDataDuplicateGroup:
    def test_known_duplicate_group_at_p3_detected(self) -> None:
        from pathlib import Path

        from reviewscope.ingestion.csv_adapter import CSVAdapter

        reviews = CSVAdapter().load(
            Path(__file__).resolve().parents[1] / "data" / "demo_reviews.csv"
        ).reviews
        p3_reviews = [r for r in reviews if r.place_id == "p3"]
        groups = DuplicateDetector().detect(p3_reviews)
        dup_count = sum(1 for g in groups if len(g.review_ids) >= 2)
        assert dup_count > 0, "expected at least one duplicate group at P3"
        assert any(g.exact_count >= 2 for g in groups), (
            "expected the 4 exact duplicates of the injected P3 group"
        )

    def test_injected_p3_group_fully_captured_with_embeddings(self) -> None:
        from pathlib import Path

        from reviewscope.embeddings.sentence_transformer import SentenceTransformerProvider
        from reviewscope.ingestion.csv_adapter import CSVAdapter

        reviews = CSVAdapter().load(
            Path(__file__).resolve().parents[1] / "data" / "demo_reviews.csv"
        ).reviews
        p3 = [r for r in reviews if r.place_id == "p3"]
        provider = SentenceTransformerProvider()
        emb = provider.encode([r.text for r in p3])
        groups = DuplicateDetector().detect(p3, embeddings=emb)
        injected = {f"rdup_{p}{i:03d}" for p in ("e", "n") for i in range(4)}
        captured = set().union(*(set(g.review_ids) for g in groups)) & injected
        assert captured == injected, f"expected all 8 injected P3 reviews, got {captured}"
