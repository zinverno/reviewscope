"""Lazy, revision-pinned SentenceTransformer provider."""

from __future__ import annotations

import re
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np

from reviewscope.config import CONFIG
from reviewscope.embeddings.base import EmbeddingProvider, EncodingContract

_PACKAGES = ("sentence-transformers", "transformers", "tokenizers", "torch")
# Declared read target for hosts without the optional backend distributions.
REFERENCE_RUNTIME = "sentence-transformers=6.1.0,transformers=5.17.0,tokenizers=0.23.2,torch=2.14.0"


@lru_cache(maxsize=1)
def installed_runtime() -> str | None:
    try:
        return ",".join(f"{package}={version(package)}" for package in _PACKAGES)
    except PackageNotFoundError:
        return None


class SentenceTransformerProvider(EmbeddingProvider):
    def __init__(
        self, model_name: str | None = None, *, revision: str | None = None,
        runtime: str | None = None,
    ) -> None:
        config = CONFIG.embedding
        name = model_name or config.model_name
        if revision is None and name == config.model_name:
            revision = config.revision
        if not revision or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("embedding model needs an immutable Hub commit SHA")
        self._contract = EncodingContract(
            model=name,
            revision=revision,
            encoder="sentence-transformers.encode/torch-cpu-fp32/sentence_embedding/no-prompt",
            max_seq_length=config.max_length,
            dim=config.dim,
            normalize_embeddings=False,
            runtime=runtime or installed_runtime() or REFERENCE_RUNTIME,
            batch_size=config.batch_size,
        )
        self._model = None

    @property
    def contract(self) -> EncodingContract:
        return self._contract

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - depends on env
                raise RuntimeError(
                    "A cache miss requires the 'embeddings' extra. See docs/DEPLOYMENT.md."
                ) from exc
            if Path(self.name).exists():
                raise ValueError("a local model path cannot attest the pinned Hub revision")
            if installed_runtime() != self.contract.runtime:
                raise ValueError("installed embedding runtime differs from the declared contract")
            model = SentenceTransformer(self.name, revision=self.contract.revision, device="cpu")
            if model.get_sentence_embedding_dimension() != self.dim:
                raise ValueError(f"model dimension differs from declared {self.dim}")
            limit = min(model.max_seq_length, model.tokenizer.model_max_length)
            if self.contract.max_seq_length > limit:
                raise ValueError(f"declared sequence length exceeds model limit {limit}")
            model.max_seq_length = self.contract.max_seq_length
            self._model = model.float()
        return self._model

    def _encode_nonblank(self, texts: list[str]) -> np.ndarray:
        return self._load().encode(
            texts,
            prompt="",
            output_value="sentence_embedding",
            precision="float32",
            batch_size=self.contract.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=self.contract.normalize_embeddings,
        )
