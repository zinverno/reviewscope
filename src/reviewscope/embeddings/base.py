"""Embedding provider ABC (SPEC.md §3).

Implementations translate raw text into numeric vectors. The provider is
identified by a stable ``name`` string so that different model versions are
not cross-cached; the ``dim`` property lets the cache know what shape to
expect without importing a provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class EmbeddingProvider(ABC):
    """Interface for all embedding backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable model identifier; used as cache key (SPEC.md §32)."""
        ...

    @property
    def dim(self) -> int:
        """The vector dimensionality; defaults to matching config."""
        return 384

    @abstractmethod
    def encode(self, texts: list[str]) -> np.ndarray:
        """Return an (n, dim) float32 array for the input texts.

        Providers must be deterministic for identical input texts and the same
        ``name`` value.
        """
        ...
