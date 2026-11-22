"""Model-free identity and output contract shared by embedding providers."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from functools import cached_property

import numpy as np

EmbeddingKey = tuple[str, str, str]


def embedding_key(review_id: str, text: str, encoding_id: str) -> EmbeddingKey:
    return review_id, hashlib.sha256(text.encode("utf-8")).hexdigest(), encoding_id


def checked_batch(values: np.ndarray, n: int, dim: int) -> np.ndarray:
    vectors = np.asarray(values, dtype=np.float32)
    if vectors.shape != (n, dim) or not np.isfinite(vectors).all():
        raise ValueError(f"encoder must return finite embeddings of shape {(n, dim)}")
    return vectors


@dataclass(frozen=True)
class EncodingContract:
    model: str
    revision: str
    encoder: str
    max_seq_length: int
    dim: int
    normalize_embeddings: bool
    runtime: str
    batch_size: int

    def __post_init__(self) -> None:
        if min(self.dim, self.max_seq_length, self.batch_size) < 1:
            raise ValueError("embedding dimension, sequence length and batch size must be positive")

    @cached_property
    def encoding_id(self) -> str:
        settings = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        # v2 hashes exact UTF-8 input and reserves a zero vector for blank text.
        return "rs-embedding-v2:" + hashlib.sha256(settings.encode()).hexdigest()


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def contract(self) -> EncodingContract:
        """Declared without loading weights; validated when the encoder starts."""

    @property
    def name(self) -> str:
        return self.contract.model

    @property
    def dim(self) -> int:
        return self.contract.dim

    def encode(self, texts: list[str]) -> np.ndarray:
        """Encode exact nonblank inputs; blank inputs get a float32 zero row."""
        result = np.zeros((len(texts), self.dim), dtype=np.float32)
        positions = [i for i, text in enumerate(texts) if text.strip()]
        if positions:
            encoded = self._encode_nonblank([texts[i] for i in positions])
            result[positions] = checked_batch(encoded, len(positions), self.dim)
        return result

    @abstractmethod
    def _encode_nonblank(self, texts: list[str]) -> np.ndarray:
        """Encode nonblank strings according to the declared contract."""
