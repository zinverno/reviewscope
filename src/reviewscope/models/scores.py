"""Score result models enforcing SPEC.md §36 explainability requirements.

Every scoring function returns a :class:`ScoreResult` which pairs the numeric
value with an evidence bag: signals, counter-signals and a confidence level.
The UI must render all four fields; it must never silently drop counter-signals.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ConfidenceLevel(StrEnum):
    """Interpretable confidence bands used across all scores."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def confidence_for_box_score(score: float, low: float, medium: float) -> ConfidenceLevel:
    """Map a 0..100 score onto LOW/MEDIUM/HIGH bands.

    Used by scoring components so that confidence semantics stay consistent:
    well above ``medium`` -> HIGH, well below ``low`` -> LOW.
    """
    if score >= medium:
        return ConfidenceLevel.HIGH
    if score >= low:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


class ScoreResult(BaseModel):
    """A single explainable score.

    Fields:
        name:      human-readable score name, e.g. "Coordinate Activity".
        value:     the score as a number. Must be clamped to 0..100 unless the
                   ``unit`` explicitly says otherwise.
        confidence: one of LOW / MEDIUM / HIGH.
        signals:   positive evidence driving the score upward.
        counter_signals: evidence pointing the other way. Never omitted when
                   counter evidence exists.
        unit:      display unit (defaults to an out-of-100 scale).
        details:   optional structured numeric breakdown used by the UI.
    """

    name: str = "Score"
    value: float = Field(ge=0.0)
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    signals: list[str] = Field(default_factory=list)
    counter_signals: list[str] = Field(default_factory=list)
    unit: str = "/100"
    details: dict[str, float] = Field(default_factory=dict)

    def add_signal(self, text: str) -> ScoreResult:
        self.signals.append(text)
        return self

    def add_counter_signal(self, text: str) -> ScoreResult:
        self.counter_signals.append(text)
        return self

    def rendered_value(self) -> str:
        """Human-readable rendering, e.g. ``"84/100"``."""
        formatted = f"{self.value:g}"
        return f"{formatted}{self.unit}"


class ComponentScore(BaseModel):
    """A single component contributing to a composite score.

    Used in the composite score explanations so the UI can show the exact
    contribution of each sub-signal (SPEC.md §14).
    """

    name: str
    score: float
    weight: float = 0.0
    signals: list[str] = Field(default_factory=list)
    counter_signals: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
