"""Run the preregistered relation experiment only on the frozen authored fixture."""

import argparse
import gzip
import hashlib
import json
import re
import resource
import socket
import time
from collections import Counter
from pathlib import Path

from scipy.sparse import csr_matrix, hstack, vstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import normalize

from reviewscope.embeddings.candidate_minilm import offline_encoder
from reviewscope.typed_relations import lexical_candidates, neighbors, text_relation
from scripts.freeze_relation_corpus import ASPECTS, ROOT, encoded

KINDS = ("same_specific_complaint", "opposite_stance")
STATES = ("C", "D", "U")
STAGES = {
    "queue": r"касс|при[её]м|прин[ия]|перед заказ|оформ|терминал|очеред",
    "handover": r"готов(?!ил)|упакова|упаковк|запечат|собран|выдач|св[её]рт|переда",
}
DENIAL = (
    r"сразу|мгнов|моменталь|немедлен|без очеред|не пришлось|не было|без промед|без задерж|тут же"
)
PROBLEM = r"долго|полчаса|ждали|минут|забы|держали|держивал|не спеш|лишь|томил|целый|едва двигал|еле полз|потерял"


def stance(text, aspect):
    matches = []
    for clause in re.finditer(r"[^.!?;\n]+", text):
        value = clause[0].lower()
        if re.search(STAGES[aspect], value):
            if re.search(DENIAL, value):
                matches.append(("D", [clause.start(), clause.end()]))
            elif re.search(PROBLEM, value):
                matches.append(("C", [clause.start(), clause.end()]))
    if len({x[0] for x in matches}) == 1:
        return matches[0]
    return "U", None


def ratio(n, d):
    return n / d if d else None


def metrics(rows, axis, method, conditional):
    labels = (
        ("exact_text", "near_copy", "no_relation")
        if axis == "text"
        else (*KINDS, "uncertain", "no_relation")
    )
    rows = [r for r in rows if r["gold"][axis] is not None and (not conditional or r["channels"])]
    conf = Counter(
        (r["gold"][axis], r["predict"][method] if r["channels"] else "not_evaluated") for r in rows
    )
    by = {}
    for label in labels:
        tp = conf[label, label]
        support = sum(v for (a, _), v in conf.items() if a == label)
        predicted = sum(v for (_, b), v in conf.items() if b == label)
        by[label] = dict(
            tp=tp,
            fp=predicted - tp,
            fn=support - tp,
            support=support,
            precision=ratio(tp, predicted),
            recall=ratio(tp, support),
            f1=ratio(2 * tp, support + predicted),
        )
    tp = sum(x["tp"] for x in by.values())
    f1 = [x["f1"] for x in by.values() if x["f1"] is not None]
    return dict(
        classes=by,
        macro_f1=ratio(sum(f1), len(f1)),
        micro_f1=ratio(2 * tp, 2 * tp + sum(x["fn"] + x["fp"] for x in by.values())),
        confusion={
            a: {b: v for (label, b), v in sorted(conf.items()) if a == label} for a in labels
        },
    )


def candidates(block, encoder):
    ids = [r["id"] for r in block]
    texts = [r["text"] for r in block]
    pairs, _ = lexical_candidates(dict(zip(ids, texts, strict=True)))
    X = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit_transform(texts)
    for name, mat, threshold in [("tfidf", (X @ X.T).toarray(), 0.1), ("minilm", None, 0.4)]:
        if mat is None:
            values = normalize(encoder.encode(texts))
            mat = values @ values.T
        for pair, rank in neighbors(ids, mat, threshold).items():
            pairs.setdefault(pair, {})[name] = rank
    return pairs


def execute(phase, out, seal):
    started = time.perf_counter()
    if {name for _, name in socket.if_nameindex()} - {"lo"}:
        raise RuntimeError("ISOLATED_NETWORK_REQUIRED")
    freeze = json.loads((ROOT / "freeze.json").read_bytes())
    for name, digest in freeze["synthetic"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    from huggingface_hub import try_to_load_from_cache

    for name, digest in freeze["minilm"].items():
        path = try_to_load_from_cache(
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            name,
            revision="e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
        )
        with open(path, "rb") as f:
            assert hashlib.file_digest(f, "sha256").hexdigest() == digest
    encoder = offline_encoder()
    reviews = [
        json.loads(x)
        for x in gzip.decompress((ROOT / "reviews.jsonl.gz").read_bytes()).splitlines()
    ]
    pairs = [
        json.loads(x) for x in gzip.decompress((ROOT / "pairs.jsonl.gz").read_bytes()).splitlines()
    ]
    docs = {r["id"]: r["text"] for r in reviews if r["split"] in {"train", phase}}
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit(
        [r["text"] for r in reviews if r["split"] == "train"]
    )
    vectors = dict(zip(docs, vec.transform(docs.values()), strict=True))
    states = {(i, axis): stance(t, axis) for i, t in docs.items() for axis in ASPECTS}
    from rapidfuzz.fuzz import ratio as fuzzy

    def features(a, b, axis):
        x, y = vectors[a], vectors[b]
        st = [states[i, axis][0] for i in (a, b)]
        return hstack(
            (
                abs(x - y),
                x.multiply(y),
                csr_matrix([[fuzzy(docs[a], docs[b]) / 100, *[st.count(k) for k in STATES]]]),
            ),
            format="csr",
        )

    heads = {}
    for axis in ASPECTS:
        train = [p for p in pairs if p["split"] == "train" and p[axis] is not None]
        heads[axis] = LogisticRegression(
            C=1, class_weight="balanced", random_state=24, max_iter=500
        ).fit(
            vstack([features(p["left"], p["right"], axis) for p in train]), [p[axis] for p in train]
        )
    weights = {
        axis: hashlib.sha256(head.coef_.tobytes() + head.intercept_.tobytes()).hexdigest()
        for axis, head in heads.items()
    }
    if phase == "test":
        chosen = json.loads(seal.read_bytes())
        assert chosen["weights"] == weights
    channels = candidates([r for r in reviews if r["split"] == phase], encoder)
    rows = []
    for p in pairs:
        if p["split"] != phase:
            continue
        a, b = p["left"], p["right"]
        kind, spans = text_relation(docs[a], docs[b])
        row = dict(
            id=p["id"],
            channels=channels.get((a, b), {}),
            gold={ax: p[ax] for ax in ("text", *ASPECTS)},
            predict={"text": kind},
            text_spans=spans,
            rules_spans={},
            scores={},
        )
        for axis in ASPECTS:
            st = [states[i, axis] for i in (a, b)]
            pair = sorted(s[0] for s in st)
            row["predict"][axis + "_rules"] = (
                KINDS[0] if pair == ["C", "C"] else KINDS[1] if pair == ["C", "D"] else "uncertain"
            )
            row["rules_spans"][axis] = [s[1] for s in st]
            x = features(a, b, axis)
            assert (x != features(b, a, axis)).nnz == 0
            logits = heads[axis].decision_function(x)[0]
            row["scores"][axis] = dict(
                zip(heads[axis].classes_, logits.round(8).tolist(), strict=True)
            )
            row["predict"][axis + "_linear"] = heads[axis].classes_[logits.argmax()]
        rows.append(row)
    report = {}
    selection = {}
    for axis in ("text", *ASPECTS):
        methods = ["text"] if axis == "text" else [axis + "_rules", axis + "_linear"]
        report[axis] = {
            "methods": {},
            "unknown": sum(r["gold"][axis] is None for r in rows),
            "retrieval": {},
        }
        for ch in ("exact", "fuzzy", "tfidf", "minilm", "union"):
            selected = [r for r in rows if r["channels"] and (ch == "union" or ch in r["channels"])]
            report[axis]["retrieval"][ch] = dict(
                selected=len(selected),
                positive_hits=dict(
                    Counter(r["gold"][axis] for r in selected if r["gold"][axis] is not None)
                ),
            )
        for method in methods:
            report[axis]["methods"][method] = {
                k: metrics(rows, axis, method, k == "conditional")
                for k in ("conditional", "end_to_end")
            }
        if axis != "text":
            selection[axis] = max(
                methods,
                key=lambda m: sum(
                    report[axis]["methods"][m]["end_to_end"]["classes"][k]["f1"] or 0 for k in KINDS
                ),
            )
    out.mkdir(exist_ok=True, parents=True)
    if phase == "calibration":
        seal.write_bytes(encoded(dict(selected=selection, weights=weights)) + b"\n")
    (out / (phase + "-pairs.jsonl.gz")).write_bytes(
        gzip.compress(b"".join(encoded(r) + b"\n" for r in rows), mtime=0)
    )
    (out / (phase + "-metrics.json")).write_bytes(encoded(report) + b"\n")
    print(
        json.dumps(
            dict(
                phase=phase,
                seconds=time.perf_counter() - started,
                cpu=time.process_time(),
                peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                encoding=encoder.contract.encoding_id,
            )
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["calibration", "test"])
    parser.add_argument("out", type=Path)
    parser.add_argument("seal", type=Path)
    args = parser.parse_args()
    execute(args.phase, args.out, args.seal)
