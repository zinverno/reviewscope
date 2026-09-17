"""Unit tests for the synthetic/templated text score (SPEC.md §16).

These tests encode the invariants from the forensic remediation:

* one well-written, polished, *unique* review must not score high (the
  detector is not an "AI detector");
* a real template *family* (shared skeleton + variable slots) shared across
  the same place's cohort must score high;
* semantic similarity alone must not imply templating: a big group of
  same-topic reviews that are specific and textually diverse stays low;
* temporal clustering alone is a small component and must not push a
  textually distinct cohort to HIGH.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from reviewscope.analysis.templated import (
    TemplatedTextScorer,
    _content_bigrams,
    _sentence_length_profile,
    templated_frame,
)
from reviewscope.models.review import NormalizedReview


def _review(
    review_id: str,
    place_id: str,
    text: str,
    day: date | None = None,
    rating: int = 5,
) -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place_id,
        reviewer_id=f"user-{review_id}",
        rating=rating,
        text=text,
        published_at=(day or date(2026, 9, 1)).isoformat(),
    )


# A single genuinely well-written review with concrete details and no cohort.
_POLISHED_UNIQUE = (
    "Безупречный сервис, неповторимая атмосфера и качество на высоте — "
    "это место восхищает во всех отношениях, мы в полном восторге!"
)

_TEMPLATED = (
    "Всё было идеально, обслуживание на высоте, отличный сервис, "
    "вкусная еда, я рекомендую всем посетить"
)


def _templated_group(n: int, place: str) -> list[NormalizedReview]:
    """Byte-identical reusable text repeated in a same-day batch."""
    start = date(2026, 9, 1)
    return [
        _review(f"t{i}", place, _TEMPLATED, start + timedelta(days=i % 5)) for i in range(n)
    ]


def _template_family(n: int, place: str) -> list[NormalizedReview]:
    """A true template family: shared skeleton with variable slot values.

    This is the pattern SPEC.md §16 must detect (reused phrases + near-peer
    cohort), while still being lexically distinct per instance.
    """
    from itertools import product

    skeletons = ["Отличное место! Здесь {a}, {b}. Я {c} — {d}"]
    slots = {
        "a": ["уютная атмосфера", "дружелюбный персонал", "быстрое обслуживание", "чисто и убрано"],
        "b": ["капучино подали горячим", "столик нашёлся сразу", "порция была щедрой"],
        "c": ["обязательно вернусь", "посоветую друзьям", "загляну ещё раз"],
        "d": ["Не пожалеете!", "Ставлю высшую оценку!", "Лучшее в городе!", "Всем рекомендую!"],
    }
    combos = [
        dict(zip(slots, vals, strict=False))
        for vals in product(*slots.values())
    ]
    return [
        _review(f"fam{i}", place, skeletons[0].format(**fill), date(2026, 9, 1))
        for i, fill in enumerate(combos[:n])
    ]


_SAME_TOPIC_DIVERSE = [
    "Заказал рибай medium rare, принесли с кровью за 18 минут, гарнир не пересолен.",
    "Пришёл в субботу вечером, ждали столик 15 минут, зато стейк пропечён идеально.",
    "Куриный бульон остыл, но хлеб из пекарни был свежим, а кофе доставили за 12 минут.",
    "Понравился дизайн интерьера и вид на набережную, брал раф на кокосовом молоке.",
    "Порция большая, счёт 2600 на двоих, окно рядом с кондиционером обдувало спинку.",
    "Официант Мария посоветовала сырную тарелку, винная карта без завышенных наценок.",
    "Стейк тартар приправили щедро, паста с трюфелем неплоха, десерт дня — чизкейк.",
    "Утка с яблоками тающая, ушли после закрытия, никто не торопил, доброй ночи.",
    "Салат с креветками свежий, суп-крем по дням разный, хлеб собственной выпечки.",
    "Отмечали повышение коллеги, кальмар гриль с цитрусовым соусом стал изюминкой.",
    "Борщ и домашние котлеты очень вкусные, персонал вежливый, но пятница шумная.",
    "Обедал дважды за командировку, американо крепкий, без сюрпризов, встречают по имени.",
    "Тартар был прохладный, как надо, столик у окна нашёлся, оплата по счет-фактуре.",
    "Винная карта приятная, официант подсказал пару к мясу, вышли на террасу с видом.",
    "Заказывал через их приложение, скидка по промокоду сработала, еда собрана за 20 минут.",
    "День рождения отмечали сюда уже третий год, десерт со свечой от шефа — база.",
    "Парковка перед входом маленькая, но валами бесплатно, внутри шумная вентиляция.",
    "Ходили с собакой, администратор принёс миску с водой, интерьер тёплый светлый.",
    "Горячее принесли на деревянной доске, стейк средней прожарки, с масляным соусом.",
    "Скидка за бронь с утра небольшая, но комплимент от шефа перекрыл — тыквенный суп.",
    "Забавно, что счёт принесли без кнопки чаевых, но оставил наличными на столике.",
    "Для командировочных отличный вариант: работает до 12 ночи, кофе крепкий и тёплый.",
    "Банкетный зал на втором этаже, звук не мешает, меню согласовали заранее по почте.",
    "Понравилось, что не навязывают допы: официант предложил воду, а не сок за 400.",
]


def _unique_reviews(n: int, place: str) -> list[NormalizedReview]:
    bodies = _SAME_TOPIC_DIVERSE[:n]
    return [
        _review(f"u{i}", place, bodies[i], date(2026, 3, i + 1), rating=4 ^ (i % 2))
        for i in range(n)
    ]


class TestHelpers:
    def test_sentence_profile_normalizes(self) -> None:
        p = _sentence_length_profile("Один. Два слова. Три коротких слова. Четыре пять шесть.")
        assert p.shape == (6,)
        assert abs(p.sum() - 1.0) < 1e-5

    def test_content_bigrams_skip_stopwords(self) -> None:
        out = _content_bigrams("вкусная еда и отличный сервис")
        assert ("вкусная", "еда") in out
        assert ("еда", "и") not in out


class TestTemplatedScorer:
    def test_single_unique_review_stays_low(self) -> None:
        reviews = _unique_reviews(1, "p1")
        [res] = TemplatedTextScorer().score(reviews)
        assert res.value < 40

    def test_small_unique_group_stays_low(self) -> None:
        reviews = _unique_reviews(4, "p1")
        results = TemplatedTextScorer().score(reviews)
        assert all(r.value < 45 for r in results)

    def test_same_topic_diverse_group_never_reaches_high(self) -> None:
        # Forensic regression: many organic reviews on the same topic and even
        # the same date must NOT be rated synthetic-like (audit §16).
        reviews = _unique_reviews(len(_SAME_TOPIC_DIVERSE), "p1")
        results = TemplatedTextScorer().score(reviews)
        assert all(r.value < 65 for r in results)
        assert all(r.confidence.value != "HIGH" for r in results)

    def test_polished_unique_review_in_organic_cohort_stays_low(self) -> None:
        # Forensic regression: one well-written, polished review among organic
        # peers must NOT be penalised for being polished (audit §16 / §20).
        reviews = _unique_reviews(20, "p1")
        polished = _review("polished", "p1", _POLISHED_UNIQUE, date(2026, 3, 14))
        reviews.append(polished)
        results = TemplatedTextScorer().score(reviews)
        res = results[reviews.index(polished)]
        assert res.value < 65
        assert res.confidence.value != "HIGH"

    def test_temporal_cluster_textually_distinct_stays_low(self) -> None:
        # Forensic regression: same-day publishing alone (weight 0.05) must
        # not raise a diverse text cohort to HIGH (audit §16 invariant 4).
        reviews = [r for r in _unique_reviews(len(_SAME_TOPIC_DIVERSE), "p1")]
        reviews = [
            _review(r.review_id, r.place_id, r.text, date(2026, 9, 2), r.rating)
            for r in reviews
        ]
        results = TemplatedTextScorer().score(reviews)
        assert all(r.value < 65 for r in results)

    def test_byte_identical_batch_scores_high(self) -> None:
        reviews = _templated_group(12, "p1")
        results = TemplatedTextScorer().score(reviews)
        assert all(r.value >= 65 for r in results)
        assert all(r.confidence.value == "HIGH" for r in results)

    def test_variable_slot_template_family_scores_high(self) -> None:
        # Forensic regression: the injected demo pattern (shared skeleton,
        # distinct slot text) must be recognised, unlike the old corpus-wide
        # semantic signal which could not distinguish it.
        reviews = _template_family(24, "p1")
        results = TemplatedTextScorer().score(reviews)
        assert all(r.value >= 65 for r in results)
        assert all(r.confidence.value == "HIGH" for r in results)

    def test_templated_group_flagged_over_crowd(self) -> None:
        # 30 highly similar polished reviews -> scores well above a lone review.
        group = _templated_group(30, "p1")
        lone = _unique_reviews(1, "p2")
        results = TemplatedTextScorer().score(group + lone)
        group_avg = np.mean([r.value for r in results[:30]])
        lone_val = results[-1].value
        assert group_avg >= 65
        assert lone_val < group_avg - 20

    def test_semantic_signal_with_embeddings(self) -> None:
        # 6 near-identical embedding vectors boost the peer multiplicity signal.
        rng = np.random.RandomState(4)
        base = rng.rand(384).astype(np.float32)
        reviews = _templated_group(6, "p1")
        embeddings = np.array([base + rng.rand(384).astype(np.float32) * 0.05 for _ in reviews])
        results = TemplatedTextScorer().score(reviews, embeddings)
        assert all(r.details["semantic"] > 0.5 for r in results)

    def test_unique_detail_signal_fires_for_reused_cohort_vocab(self) -> None:
        # Low unique-detail density (cohort reusing the same content words)
        # must register, while raw TTR must not be relied on (audit §16).
        reviews = _templated_group(12, "p1")
        results = TemplatedTextScorer().score(reviews)
        assert all(r.details["unique_detail"] > 0.5 for r in results)

    def test_empty(self) -> None:
        assert TemplatedTextScorer().score([]) == []

    def test_temporal_signal(self) -> None:
        reviews = _templated_group(6, "p1")
        results = TemplatedTextScorer().score(reviews)
        assert any(r.details["temporal"] > 0.5 for r in results)


def test_templated_frame() -> None:
    reviews = _templated_group(5, "p1")
    results = TemplatedTextScorer().score(reviews)
    frame = templated_frame(reviews, results)
    assert list(frame.columns) == ["review_id", "templated_score", "confidence"]
    assert len(frame) == 5
