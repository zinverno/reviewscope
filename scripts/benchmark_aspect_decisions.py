"""Offline PR-22 experiment. Fit/calibrate all frozen heads before opening test."""

import argparse
import gzip
import hashlib
import json
import os
import resource
import socket
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from time import monotonic, process_time

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import normalize

from reviewscope.aspect_decisions import (
    COMPLAINTS,
    LABELS,
    UNDECIDED,
    aggregate,
    definitions,
    lexical_encoder,
    windows,
)
from reviewscope.embeddings.sentence_transformer import REFERENCE_RUNTIME, installed_runtime

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/data/fixtures"
REV = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
METHODS = ("lexical", "minilm", "pair")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_rows(splits):
    for line in (FIX / "decision-freeze.sha256").read_text().splitlines():
        expected, path = line.split()
        if digest(ROOT / path) != expected:
            raise ValueError("INVALID_FREEZE")
    rows = [json.loads(s) for s in (FIX / "decision_reviews_v1.jsonl").read_text().splitlines()]
    refs = []
    for r in rows:
        refs.append((r["id"], r["family"], " ".join(r["text"].casefold().split())))
        if r["split"] not in splits:
            continue
        if set(r["labels"]) != set(definitions()):
            raise ValueError("INCOMPLETE_GOLD")
        for label in r["labels"].values():
            if (
                label["outcome"] not in (*LABELS, "unknown")
                or any(
                    not 0 <= a < b <= len(r["text"]) or r["text"][a:b] != s
                    for a, b, s in label["spans"]
                )
                or bool(label["spans"]) == (label["outcome"] == "not_mentioned")
            ):
                raise ValueError("INVALID_GOLD")
    if any(len(set(series)) != len(rows) for series in zip(*refs, strict=True)):
        raise ValueError("SPLIT_LEAK")
    return [r for r in rows if r["split"] in splits]


def ratio(n, d):
    return n / d if d else None


def binary(rows):
    known = [r for r in rows if r["gold"] not in UNDECIDED | {"unknown"}]
    tp = sum(r["gold"] in COMPLAINTS and r["outcome"] in COMPLAINTS for r in known)
    fp = sum(r["gold"] not in COMPLAINTS and r["outcome"] in COMPLAINTS for r in known)
    fn = sum(r["gold"] in COMPLAINTS and r["outcome"] not in COMPLAINTS for r in known)
    abstained_negative = sum(
        r["gold"] not in COMPLAINTS and r["outcome"] in UNDECIDED for r in known
    )
    return dict(
        tp=tp,
        fp=fp,
        fn=fn,
        tn=len(known) - tp - fp - fn - abstained_negative,
        abstained_negative=abstained_negative,
        known=len(known),
        precision=ratio(tp, tp + fp),
        recall=ratio(tp, tp + fn),
        f1=ratio(2 * tp, 2 * tp + fp + fn),
    )


def confusion(rows, classify):
    counts = Counter(
        (classify(r["gold"]), classify(r["outcome"])) for r in rows if r["gold"] != "unknown"
    )
    classes = sorted({k for pair in counts for k in pair})
    out = {}
    for key in classes:
        tp = counts[key, key]
        positives = sum(v for (a, _), v in counts.items() if a == key)
        predicted = sum(v for (_, b), v in counts.items() if b == key)
        out[key] = dict(
            support=positives,
            precision=ratio(tp, predicted),
            recall=ratio(tp, positives),
            f1=ratio(2 * tp, positives + predicted),
        )
    macro = {
        m: np.mean([v[m] for v in out.values() if v[m] is not None]).item()
        if any(v[m] is not None for v in out.values())
        else None
        for m in ("precision", "recall", "f1")
    }
    return dict(
        matrix={k: {p: counts[k, p] for p in classes} for k in classes},
        per_class=out,
        macro=macro,
        micro_prf=ratio(sum(counts[k, k] for k in classes), sum(counts.values())),
    )


def metrics(rows):
    def relevance(label):
        return label if label in {"not_mentioned", "topic_uncertain"} else "mentioned"

    defined = [r for r in rows if r["outcome"] not in UNDECIDED]
    denials = [r for r in rows if r["gold"] == "denial"]
    mixed = [r for r in rows if r["gold"] == "mixed"]
    tail = [r for r in rows if r["length"] > 160 and r["gold"] in COMPLAINTS]
    reps = {a: [r for r in rows if r["aspect"] == a] for a in definitions()}
    return dict(
        n=len(rows),
        evaluated=len(rows),
        defined=len(defined),
        coverage=ratio(len(defined), len(rows)),
        unknown=sum(r["gold"] == "unknown" for r in rows),
        unknown_polarity=sum(r["gold"] in UNDECIDED | {"unknown"} for r in rows),
        abstention_gold=dict(Counter(r["gold"] for r in rows if r["outcome"] in UNDECIDED)),
        complaint=binary(rows),
        selective=binary(defined),
        per_aspect={
            k: binary(v)
            | {
                "n": len(v),
                "coverage": ratio(sum(r["outcome"] not in UNDECIDED for r in v), len(v)),
            }
            for k, v in reps.items()
        },
        outcome=confusion(rows, lambda x: x),
        relevance=confusion(rows, relevance),
        stance=confusion([r for r in rows if relevance(r["gold"]) == "mentioned"], lambda x: x),
        denial_fp=[sum(r["outcome"] in COMPLAINTS for r in denials), len(denials)],
        mixed_recall=[sum(r["outcome"] in COMPLAINTS for r in mixed), len(mixed)],
        tail_recall=[sum(r["outcome"] in COMPLAINTS for r in tail), len(tail)],
    )


class Features:
    def __init__(self, method):
        self.method = method
        self.lexical = lexical_encoder()
        self.cache = {}
        self.tokens = 0
        self.window_count = 0
        self.encode_s = self.segment_s = 0.0
        if method == "lexical":
            return
        if {n for _, n in socket.if_nameindex()} - {"lo"} or any(
            os.getenv(k) != "1" for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
        ):
            raise ValueError("OFFLINE_REQUIRED")
        if installed_runtime() != REFERENCE_RUNTIME:
            raise ValueError("INCOMPATIBLE_ENCODING")
        import torch
        from huggingface_hub import try_to_load_from_cache
        from transformers import AutoModel, AutoTokenizer

        weights = try_to_load_from_cache(MODEL, "model.safetensors", revision=REV)
        if not isinstance(weights, str):
            raise ValueError("NOT_RUN_MISSING_WEIGHTS")
        path = Path(weights).parent
        specs = json.loads(
            (ROOT / "docs/development/rerank-evidence/minilm-cache.json").read_text()
        )
        if {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()} != specs.keys():
            raise ValueError("INCOMPATIBLE_ENCODING")
        for name, spec in specs.items():
            if (path / name).stat().st_size != spec["bytes"] or digest(path / name) != spec[
                "sha256"
            ]:
                raise ValueError("INCOMPATIBLE_ENCODING")
        opts = dict(local_files_only=True, trust_remote_code=False)
        self.tokenizer = AutoTokenizer.from_pretrained(path, **opts)
        torch.set_num_threads(1)
        self.model = (
            AutoModel.from_pretrained(path, use_safetensors=True, **opts).cpu().float().eval()
        )
        self.torch = torch

    def spans(self, text, definition=None):
        if self.method == "lexical":
            return list(windows(text))

        def fit(a, b):
            piece = text[a:b]
            if not piece.strip():
                return []
            ids = self.tokenizer(
                piece if definition is None else definition,
                text_pair=None if definition is None else piece,
                truncation=False,
                verbose=False,
            )
            if len(ids.input_ids) <= 128:
                return [(a, b)]
            if b - a <= 1:
                raise ValueError("DEFINITION_TOKEN_LIMIT")
            middle = (a + b) // 2
            return fit(a, middle) + fit(middle, b)

        return [span for a, b in windows(text) for span in fit(a, b)]

    def encode(self, texts, prefix=None):
        unique = list(dict.fromkeys(t for t in texts if (prefix, t) not in self.cache))
        for i in range(0, len(unique), 8):
            batch = unique[i : i + 8]
            inputs = self.tokenizer(
                batch if prefix is None else [prefix] * len(batch),
                text_pair=None if prefix is None else batch,
                padding=True,
                truncation=False,
                return_tensors="pt",
            )
            if inputs.input_ids.shape[1] > 128:
                raise ValueError("TOKEN_LIMIT")
            self.tokens += int(inputs.attention_mask.sum())
            with self.torch.inference_mode():
                hidden = self.model(**inputs).last_hidden_state
                mask = inputs.attention_mask.bool()
                if prefix is not None:
                    mask = self.torch.tensor(
                        [[s == 1 for s in inputs.sequence_ids(j)] for j in range(len(batch))]
                    )
                mask = mask.unsqueeze(-1)
                if (mask.sum(1) == 0).any():
                    raise ValueError("EMPTY_PAIR_CONTEXT")
                vec = self.torch.nn.functional.normalize((hidden * mask).sum(1) / mask.sum(1))
            for t, v in zip(batch, vec.numpy(), strict=True):
                self.cache[prefix, t] = v
        return np.stack([self.cache[prefix, t] for t in texts])

    def compute(self, rows, aspect):
        d = definitions()[aspect]
        prefix = d["definition"] if self.method == "pair" else None
        before = monotonic()
        parts = [self.spans(r["text"], prefix) for r in rows]
        self.segment_s += monotonic() - before
        before = monotonic()
        texts = [r["text"][a:b] for r, sp in zip(rows, parts, strict=True) for a, b in sp]
        self.window_count += len(texts)
        if self.method == "lexical":
            feats = self.lexical.transform(texts)
        elif self.method == "pair":
            feats = self.encode(texts, prefix)
        else:
            vec = self.encode(texts)
            fields = [d["name"], d["definition"], *d["include"], *d["exclude"]]
            phrases = [field[a:b] for field in fields for a, b in self.spans(field)]
            dv = normalize(self.encode(phrases).mean(axis=0, keepdims=True))[0]
            feats = np.concatenate([vec, vec * dv, np.abs(vec - dv)], axis=1)
        self.encode_s += monotonic() - before
        return parts, feats


def fit_head(features, parts, rows, aspect):
    labels = []
    for r, spans in zip(rows, parts, strict=True):
        label = r["labels"][aspect]
        for a, b in spans:
            labels.append(
                label["outcome"]
                if any(a < d and c < b for c, d, _ in label["spans"])
                else "not_mentioned"
            )
    mask = np.array(labels) != "unknown"
    model = LogisticRegression(C=3, class_weight="balanced", max_iter=500, random_state=2201)
    model.fit(features[mask], np.array(labels)[mask])
    weights = np.zeros((len(LABELS), features.shape[1]))
    bias = np.full(len(LABELS), -1.0e8)
    for label, coef, intercept in zip(model.classes_, model.coef_, model.intercept_, strict=True):
        weights[LABELS.index(label)], bias[LABELS.index(label)] = coef, intercept
    return weights, bias


def observations(rows, aspect, parts, logits, threshold):
    result = []
    pos = 0
    for r, spans in zip(rows, parts, strict=True):
        values = logits[pos : pos + len(spans)]
        result.append(
            dict(
                id=r["id"],
                aspect=aspect,
                gold=r["labels"][aspect]["outcome"],
                length=len(r["text"]),
                windows=spans,
                logits=values.tolist(),
                **aggregate(spans, values, threshold),
            )
        )
        pos += len(spans)
    if pos != len(logits):
        raise ValueError("MEMBERSHIP_MISMATCH")
    return result


def write_json(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def run(out, phase, method):
    begin, cpu = monotonic(), process_time()
    rows = load_rows({"train", "cal"} if phase == "fit" else {"test"})
    model = Features(method)
    prep = monotonic() - begin
    train = [r for r in rows if r["split"] == "train"]
    target = [r for r in rows if r["split"] != "train"]
    trained = {}
    fit_s = infer_s = aggregate_s = 0.0
    raw = {}
    if phase == "test":
        lock = json.loads((out / "selection.json").read_text())["methods"][method]
        if digest(out / f"{method}.npz") != lock["sha256"]:
            raise ValueError("INVALID_HEAD")
        with np.load(out / f"{method}.npz", allow_pickle=False) as data:
            trained = dict(data)
    for aspect in definitions():
        if phase == "fit":
            parts, x = model.compute(train, aspect)
            before = monotonic()
            trained["coef_" + aspect], trained["bias_" + aspect] = fit_head(x, parts, train, aspect)
            fit_s += monotonic() - before
        parts, x = model.compute(target, aspect)
        before = monotonic()
        raw[aspect] = parts, x @ trained["coef_" + aspect].T + trained["bias_" + aspect]
        infer_s += monotonic() - before
    before = monotonic()

    def predict(threshold):
        return [r for a, (p, v) in raw.items() for r in observations(target, a, p, v, threshold)]

    if phase == "fit":
        grid = {}
        for threshold in (0.0, 0.15, 0.4, 0.8):
            m = metrics(predict(threshold))
            grid[threshold] = (m["complaint"]["f1"] or 0.0, m["coverage"])
        best = max(grid, key=lambda t: (*grid[t], -t))
        trained["threshold"] = np.array(best)
        np.savez_compressed(out / f"{method}.npz", **trained)
        info = dict(
            grid=grid,
            threshold=best,
            sha256=digest(out / f"{method}.npz"),
            calibration_f1=grid[best][0],
            coverage=grid[best][1],
        )
        write_json(out / f"{method}-fit.json", info)
    else:
        best = float(trained["threshold"])
    results = predict(best)
    aggregate_s += monotonic() - before
    data = "".join(
        json.dumps(r, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        for r in results
    ).encode()
    (out / f"{method}-{phase}-rows.jsonl.gz").write_bytes(gzip.compress(data, mtime=0))
    write_json(out / f"{method}-{phase}-metrics.json", metrics(results))
    write_json(
        out / f"{method}-{phase}-resource.json",
        dict(
            prepare_s=prep,
            wall_s=monotonic() - begin,
            cpu_s=process_time() - cpu,
            fit_s=fit_s,
            infer_s=infer_s,
            aggregate_s=aggregate_s,
            encode_s=model.encode_s,
            segment_s=model.segment_s,
            windows=model.window_count,
            tokens=model.tokens or None,
            unique_encoded=len(model.cache) if method != "lexical" else None,
            rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            sklearn=version("scikit-learn"),
            numpy=version("numpy"),
            ml_runtime=None if method == "lexical" else installed_runtime(),
            results=len(results),
        ),
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    p.add_argument("phase", choices=["fit", "select", "test"])
    p.add_argument("method", nargs="?", choices=METHODS)
    args = p.parse_args()
    args.output.mkdir(exist_ok=True, parents=True)
    if args.phase == "select":
        methods = {m: json.loads((args.output / f"{m}-fit.json").read_text()) for m in METHODS}
        winner = max(methods, key=lambda m: (methods[m]["calibration_f1"], methods[m]["coverage"]))
        write_json(args.output / "selection.json", dict(selected=winner, methods=methods))
    elif args.method:
        run(args.output, args.phase, args.method)
    else:
        p.error("fit and test require a method")


if __name__ == "__main__":
    main()
