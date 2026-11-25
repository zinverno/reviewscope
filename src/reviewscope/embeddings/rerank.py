"""Pinned CPU-only PR-21 adapters. No ML imports in the cabinet web process."""

import hashlib
import json
import os
import socket
from pathlib import Path

import numpy as np

from reviewscope.embeddings.base import EncodingContract
from reviewscope.embeddings.sentence_transformer import REFERENCE_RUNTIME, installed_runtime

MODELS = json.loads(Path(__file__).with_name("rerank_models.json").read_text())
BATCH = 8
PROFILE = "mmarco-context-logit-v1"
FALLBACK = {
    "NOT_RUN_MISSING_BACKEND",
    "NOT_RUN_MISSING_WEIGHTS",
    "INCOMPATIBLE_MODEL",
    "QUERY_TOKEN_LIMIT",
    "ANCHOR_TOKEN_LIMIT",
    "PAIR_TOKEN_LIMIT",
}


def verified_snapshot(key):
    if key not in MODELS:
        raise ValueError("INVALID_PROFILE")
    if any(os.environ.get(k) != "1" for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")) or (
        {name for _, name in socket.if_nameindex()} - {"lo"}
    ):
        raise ValueError("OFFLINE_REQUIRED")
    root = os.getenv("RS_RERANK_MODELS")
    model = MODELS[key]
    path = Path(root) / model["revision"] if root else None
    if not path or not path.is_dir():
        raise ValueError("NOT_RUN_MISSING_WEIGHTS")
    if {p.name for p in path.iterdir()} != set(model["files"]):
        raise ValueError("INCOMPATIBLE_MODEL")
    for name, spec in model["files"].items():
        file = path / name
        if file.is_symlink() or not file.is_file() or file.stat().st_size != spec["bytes"]:
            raise ValueError("INCOMPATIBLE_MODEL")
        with file.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != spec["sha256"]:
                raise ValueError("INCOMPATIBLE_MODEL")
    return path


class LocalModel:
    def __init__(self, key):
        path = verified_snapshot(key)
        runtime = installed_runtime()
        if runtime is None:
            raise ValueError("NOT_RUN_MISSING_BACKEND")
        if runtime != REFERENCE_RUNTIME:
            raise ValueError("INCOMPATIBLE_MODEL")
        try:
            import torch
            from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise ValueError("NOT_RUN_MISSING_BACKEND") from exc
        torch.set_num_threads(1)
        options = dict(local_files_only=True, trust_remote_code=False)
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(path, **options)
            loader = AutoModel if key == "e5" else AutoModelForSequenceClassification
            self.model = (
                loader.from_pretrained(path, **options, use_safetensors=True).cpu().float().eval()
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("INCOMPATIBLE_MODEL") from exc
        self.torch = torch

    def length(self, text, pair=None):
        return len(
            self.tokenizer(text, text_pair=pair, truncation=False, verbose=False)["input_ids"]
        )


class E5(LocalModel):
    def __init__(self):
        super().__init__("e5")
        spec = MODELS["e5"]
        self.contract = EncodingContract(
            spec["model"],
            spec["revision"],
            "transformers/cpu-fp32/masked-mean/passage-query-asymmetric-v1",
            128,
            384,
            True,
            installed_runtime(),
            BATCH,
        )

    def token_count(self, text):
        return self.length("passage: " + text)

    def query_count(self, text):
        return self.length("query: " + text)

    def encode(self, texts, *, prefix="passage: "):
        output = []
        for start in range(0, len(texts), BATCH):
            batch = self.tokenizer(
                [prefix + t for t in texts[start : start + BATCH]],
                padding=True,
                truncation=False,
                return_tensors="pt",
            )
            if batch.input_ids.shape[1] > 128:
                raise ValueError(
                    "QUERY_TOKEN_LIMIT" if prefix == "query: " else "ANCHOR_TOKEN_LIMIT"
                )
            with self.torch.inference_mode():
                hidden = self.model(**batch).last_hidden_state
                mask = batch.attention_mask.unsqueeze(-1)
                vectors = (hidden * mask).sum(1) / mask.sum(1)
                output.append(self.torch.nn.functional.normalize(vectors).numpy())
        return np.concatenate(output)

    def encode_query(self, texts):
        return self.encode(texts, prefix="query: ")


class CrossEncoder(LocalModel):
    def __init__(self):
        super().__init__("ce")

    def score(self, query, windows):
        if self.length(query) > 128:
            raise ValueError("QUERY_TOKEN_LIMIT")
        scores = {}
        for start in range(0, len(windows), BATCH):
            batch = windows[start : start + BATCH]
            for _, anchor, _ in batch:
                if self.length(anchor) > 128:
                    raise ValueError("ANCHOR_TOKEN_LIMIT")
            inputs = self.tokenizer(
                [query] * len(batch),
                [w[2] for w in batch],
                padding=True,
                truncation=False,
                return_tensors="pt",
            )
            if inputs.input_ids.shape[1] > 512:
                raise ValueError("PAIR_TOKEN_LIMIT")
            with self.torch.inference_mode():
                logits = self.model(**inputs).logits.numpy()
            if (
                logits.dtype != np.float32
                or logits.shape != (len(batch), 1)
                or not np.isfinite(logits).all()
            ):
                raise ValueError("INCOMPATIBLE_MODEL")
            scores.update((w[0], float(row[0])) for w, row in zip(batch, logits, strict=True))
        return scores
