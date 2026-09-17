"""Heuristic review specificity score (SPEC.md §17).

The score reflects how informative a review is on a 0..100 scale. It rewards
concrete signals (numbers, wait times, dish/service mentions, detail-rich
narrative) and penalizes generic filler language and ultra-short texts.

This is deliberately a lightweight rule-based scorer: no LLM dependency
(SPEC.md §17).
"""

from __future__ import annotations

import re

from reviewscope.config import CONFIG, SpecificityConfig
from reviewscope.models.scores import ConfidenceLevel, ScoreResult

_NUMBER_RE = re.compile(r"\d+")
_LONG_WORD_RE = re.compile(r"\b\w{6,}\b", re.UNICODE)
_WAIT_RE = re.compile(r"\b(минут|минута|минуту|час|часа|часов|секунд|неделю)\b", re.IGNORECASE)
#: Menu/product/service mentions that make a review more specific.
_MENU_PRODUCT = (
    "капучино",
    "латте",
    "эспрессо",
    "раф",
    "американо",
    "стейк",
    "борщ",
    "котлет",
    "чизкейк",
    "десерт",
    "пицца",
    "суши",
    "паста",
    "трюфель",
    "рибай",
    "татар",
    "молок",
    "лови кофе",
    "supplement",
    "abonement",
    "абонемент",
    "тренировк",
    "кардио",
    "хотел",
    "номер",
    "wi-fi",
    "койка",
    "ноутбук",
    "аккумулятор",
    "чека",
    "гарантия",
    "английский",
    "анестези",
    "корень",
    "зуб",
    "снимок",
)
_ENTITY_WORDS = (
    "администратор",
    "официант",
    "бариста",
    "врач",
    "стоматолог",
    "тренер",
    "консультант",
    "горничн",
    "ресепшн",
    "персонал",
)
_NARRATIVE_WORDS = (
    "потом",
    "хотя",
    "потому",
    "когда",
    "после",
    "затем",
    "однако",
    "несмотря",
    "прежде",
    "наконец",
    "показалось",
    "пришлось",
    "заказал",
    "ждал",
    "ждала",
    "приехал",
    "сходил",
    "попросил",
)
#: Generic filler words/expressions that reduce specificity.
_GENERIC_PHRASES = (
    "всё понравилось",
    "все понравилось",
    "все супер",
    "все отлично",
    "всё отлично",
    "рекомендую всем",
    "рекомендую всем",
    "лучшее место",
    "лучше не найти",
    "безупречно",
    "идеально",
    "на высоте",
    "не передать словами",
    "незабываемые впечатления",
    "было волшебно",
    "это просто супер",
    "нет слов",
    "не пожалеете",
    "останетесь довольны",
    "великолепно",
    "восхитительно",
    "потрясающе",
    "top place",
    "everything was perfect",
    "highly recommend",
    "love it",
    "amazing",
    "awesome",
    "excellent place",
    "замечательно",
    "шикарно",
    "супер",
    "отлично",
    "класс",
)


def specificity_score(text: str, config: SpecificityConfig = CONFIG.specificity) -> ScoreResult:
    """Compute the specificity score for a single review text (0..100)."""
    signals: list[str] = []
    counter_signals: list[str] = []
    score = 50.0
    lowered = (text or "").lower()

    if not lowered.strip():
        return ScoreResult(
            name="Specificity",
            value=0.0,
            confidence=ConfidenceLevel.LOW,
            signals=["empty text"],
            counter_signals=[],
        )

    n_chars = len(lowered)

    if _NUMBER_RE.search(lowered):
        score += config.signal_points["numbers"]
        signals.append("contains concrete numbers/time references")
    else:
        counter_signals.append("no numeric detail")

    long_words = _LONG_WORD_RE.findall(lowered)
    if len(long_words) >= 5:
        score += config.signal_points["concrete_entities"]
        signals.append("contains specific long-word entities")
    else:
        counter_signals.append("low entity density")

    if _WAIT_RE.search(lowered):
        score += config.signal_points["wait_time"]
        signals.append("mentions wait times")

    if any(phrase in lowered for phrase in _MENU_PRODUCT):
        score += config.signal_points["menu_product"]
        signals.append("specific product/service details")

    if any(phrase in lowered for phrase in _ENTITY_WORDS):
        score += config.signal_points["noun_details"]
        signals.append("identifies involved staff/persons")

    narrative_hits = [w for w in _NARRATIVE_WORDS if w in lowered]
    if narrative_hits:
        score += config.signal_points["narrative_words"]
        signals.append("narrative event description")

    if n_chars > 200:
        score += config.signal_points["length_bonus"]
        signals.append("detailed text")
    elif n_chars < 50:
        score *= config.short_text_factor
        counter_signals.append("very short text")

    generic_hits = [p for p in _GENERIC_PHRASES if p in lowered]
    if generic_hits:
        penalty = config.generic_penalty * (1 + 0.25 * (len(generic_hits) - 1))
        score -= min(penalty, config.max_missing_penalty)
        counter_signals.append(f"generic language: {', '.join(generic_hits[:3])}")

    score = max(0.0, min(100.0, score))
    confidence = ConfidenceLevel.HIGH if score >= 65 else (
        ConfidenceLevel.MEDIUM if score >= 40 else ConfidenceLevel.LOW
    )
    return ScoreResult(
        name="Specificity",
        value=round(score, 1),
        confidence=confidence,
        signals=signals,
        counter_signals=counter_signals,
    )
