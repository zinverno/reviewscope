"""Sentence-transformer embedding provider (SPEC.md §3).

Uses ``paraphrase-multilingual-MiniLM-L12-v2`` by default, suitable for both
Russian and English. The provider is stateless between calls; the model is
loaded once and cached on the instance.
"""

from __future__ import annotations

import numpy as np
import torch

from reviewscope.config import CONFIG
from reviewscope.embeddings.base import EmbeddingProvider


class SentenceTransformerProvider(EmbeddingProvider):
    """Multilingual sentence-transformer embedding provider."""

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or CONFIG.embedding.model_name
        self._model = None

    @property
    def name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        return CONFIG.embedding.dim

    def _load(self):  # noqa: ANN202
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        """Encode a list of texts into a (n, dim) float32 embedding matrix.

        Empty or whitespace-only texts are encoded to zero vectors so that
        the output shape remains predictable and downstream consumers never
        encounter ``None`` values.
        """
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        model = self._load()
        with torch.inference_mode():
            embeddings = model.encode(
                texts,
                batch_size=CONFIG.embedding.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=False,
            )
        return np.asarray(embeddings, dtype=np.float32)
