"""Load the PR-04 contract only in an explicitly offline retrieval process."""

import os
from pathlib import Path

from reviewscope.embeddings.sentence_transformer import (
    SentenceTransformerProvider,
    installed_runtime,
)


class CandidateMiniLM(SentenceTransformerProvider):
    def token_count(self, text):
        tokenizer = self._load().tokenizer
        return len(
            tokenizer(text, truncation=False, add_special_tokens=True, verbose=False)["input_ids"]
        )


def offline_encoder():
    if any(os.environ.get(k) != "1" for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")):
        raise ValueError("OFFLINE_REQUIRED")
    if installed_runtime() is None:
        raise ValueError("NOT_RUN_MISSING_BACKEND")
    from huggingface_hub import constants, try_to_load_from_cache

    if not constants.HF_HUB_OFFLINE:
        raise ValueError("OFFLINE_REQUIRED")
    encoder = CandidateMiniLM()
    weights = try_to_load_from_cache(
        encoder.name, "model.safetensors", revision=encoder.contract.revision
    )
    if not isinstance(weights, str) or not Path(weights).is_file():
        raise ValueError("NOT_RUN_MISSING_WEIGHTS")
    try:
        encoder._load()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("INCOMPATIBLE_ENCODING") from exc
    return encoder
