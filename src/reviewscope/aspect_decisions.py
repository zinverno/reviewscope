"""Experimental whole-review decision on one exact, authored PR-20 definition."""

import hashlib
import json
from collections.abc import Iterator
from importlib.resources import files

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

PROFILE = "aspect-linear-lexical-v1"
THRESHOLD_SET = "calibrated-v1"
LABELS = (
    "complaint",
    "mixed",
    "denial",
    "positive",
    "neutral",
    "unclear",
    "not_mentioned",
    "topic_uncertain",
)
UNDECIDED = {"unclear", "topic_uncertain"}
COMPLAINTS = {"complaint", "mixed"}
ASSETS = files("reviewscope").joinpath("resources")
WEIGHTS_SHA256 = "7e0844814c5a0a363e8fee00740f06c46741c0123f285c6f46d1d1aa66aa65c8"
DEFINITIONS_SHA256 = "b0124a7d6e94873bcb7d40450c8422c8dbb74b5cadb20aa808dd0e1c3e76bc81"
FEATURES = 1024


def definitions():
    data = ASSETS.joinpath("decision-definitions.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != DEFINITIONS_SHA256:
        raise ValueError("INCOMPATIBLE_MODEL")
    return json.loads(data)


def windows(text: str) -> Iterator[tuple[int, int]]:
    for start in range(0, len(text), 112):
        end = min(start + 160, len(text))
        if text[start:end].strip():
            yield start, end
        if end == len(text):
            break


def lexical_encoder():
    return HashingVectorizer(
        analyzer="char_wb", ngram_range=(3, 5), n_features=FEATURES, alternate_sign=False
    )


def aggregate(spans, logits, threshold):
    """Choose once per review, including abstention. Spans are model context."""
    if not len(spans) or logits.shape != (len(spans), len(LABELS)) or not np.isfinite(logits).all():
        raise ValueError("INVALID_MODEL_OUTPUT")
    observations = []
    for (start, end), values in zip(spans, logits, strict=True):
        rank = np.argsort(-values, kind="stable")
        outcome = LABELS[rank[0]]
        margin = float(values[rank[0]] - values[rank[1]])
        reason = "MODEL_CONTEXT"
        if margin < threshold:
            outcome = (
                "topic_uncertain" if outcome in {"not_mentioned", "topic_uncertain"} else "unclear"
            )
            reason = "LOW_MARGIN"
        observations.append(
            dict(outcome=outcome, score=margin, start=start, end=end, reason=reason)
        )

    def order(row):
        label = row["outcome"]
        return (
            label in COMPLAINTS,
            label not in {"topic_uncertain", "not_mentioned"},
            label != "not_mentioned",
            row["score"],
            -row["start"],
        )

    best = max(observations, key=order).copy()
    counters = [
        r
        for r in observations
        if r["outcome"] in {"denial", "positive"}
        and (r["end"] <= best["start"] or r["start"] >= best["end"])
    ]
    best.update(counter_start=None, counter_end=None)
    if best["outcome"] == "complaint" and counters:
        counter = max(counters, key=order)
        best.update(
            outcome="mixed",
            reason="DISJOINT_STANCES",
            counter_start=counter["start"],
            counter_end=counter["end"],
        )
    if best["outcome"] == "not_mentioned":
        best.update(start=None, end=None, reason="ALL_WINDOWS_OFF_TOPIC")
    return best


class DecisionScorer:
    threshold_set = THRESHOLD_SET
    profile_sha256 = hashlib.sha256(
        "|".join(
            (
                PROFILE,
                THRESHOLD_SET,
                "hash-charwb3-5-1024-l2/160-stride112/logit-margin/aggregate-1",
                DEFINITIONS_SHA256,
                WEIGHTS_SHA256,
            )
        ).encode()
    ).hexdigest()

    def __init__(self, definition: dict, profile: str):
        if profile != PROFILE:
            raise ValueError("UNSUPPORTED_PROFILE")
        supported = definitions()
        aspect = next((k for k, v in supported.items() if v == definition.get("payload")), None)
        if not aspect or definition.get("origin") != "manual":
            raise ValueError("UNSUPPORTED_DEFINITION")
        path = ASSETS.joinpath("decision-lexical-v1.npz")
        if not path.is_file():
            raise ValueError("MODEL_UNAVAILABLE")
        if (
            path.stat().st_size > 524288
            or hashlib.sha256(path.read_bytes()).hexdigest() != WEIGHTS_SHA256
        ):
            raise ValueError("INCOMPATIBLE_MODEL")
        with np.load(path, allow_pickle=False) as data:
            self.coef = data["coef_" + aspect]
            self.bias = data["bias_" + aspect]
            self.threshold = float(data["threshold"].item())
        if (
            self.coef.shape != (len(LABELS), FEATURES)
            or self.bias.shape != (len(LABELS),)
            or not np.isfinite(self.coef).all()
            or not np.isfinite(self.bias).all()
            or self.threshold not in (0.0, 0.15, 0.4, 0.8)
        ):
            raise ValueError("INCOMPATIBLE_MODEL")
        self.encoder = lexical_encoder()

    def score(self, text: str):
        if not text.strip():
            raise ValueError("EXCLUDED_BLANK")
        spans = list(windows(text))
        features = self.encoder.transform(text[start:end] for start, end in spans)
        return aggregate(spans, features @ self.coef.T + self.bias, self.threshold)
