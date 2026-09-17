"""Static stopword resources shipped with the package.

SPEC.md §9 requires basic Russian and English keyword analysis, and engineering
requirement 6 forbids mandatory runtime downloads for basic stopword data.
These files satisfy both: they are full, static word lists bundled in the wheel.
"""

from __future__ import annotations

from pathlib import Path

RESOURCES_DIR = Path(__file__).parent


def _load(name: str) -> set[str]:
    text = (RESOURCES_DIR / name).read_text(encoding="utf-8")
    return {word.strip().lower() for word in text.splitlines() if word.strip()}


EN_STOPWORDS: set[str] = _load("en_stopwords.txt")
RU_STOPWORDS: set[str] = _load("ru_stopwords.txt")

__all__ = ["EN_STOPWORDS", "RU_STOPWORDS"]
