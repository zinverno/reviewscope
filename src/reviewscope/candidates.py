"""Exact candidate selection on one immutable corpus. All offsets are Unicode code points."""

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.metadata import version
from itertools import chain
from time import perf_counter

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

from reviewscope.embeddings.base import checked_batch
from reviewscope.validation.retrieval_dataset import digest

SPEC = "fragmentation-v1"
BOUNDARY = re.compile(r'(?:[.!?…]+[»”"’)]*(?=\s|$)|\r\n|[\r\n])')
MODES = {"lexical", "dense", "hybrid"}
ROLES = {"query_to_passage", "text_to_text"}
CHANNEL_BUDGET = 50
MAX_FRAGMENTS = 60000


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Document:
    version_id: str
    content_sha256: str
    text: str


@dataclass(frozen=True)
class Span:
    version_id: str
    content_sha256: str
    text_sha256: str
    fragment_id: str
    start: int
    end: int
    context_start: int
    context_end: int


def anchors(text, count_tokens=None):
    """Keep sentence punctuation and interior whitespace. Bound one sentence before tokenizing."""
    spans = []
    start = 0
    for end in chain((m.end() for m in BOUNDARY.finditer(text)), (len(text),)):
        while start < end:
            while start < end and text[start].isspace():
                start += 1
            if start == end:
                break
            stop = min(start + 240, end)
            while True:
                if stop < end:
                    split = max(
                        (i for i in range(start + 1, stop) if text[i].isspace()), default=stop
                    )
                else:
                    split = stop
                piece = text[start:split].rstrip()
                if count_tokens is None or count_tokens(piece) <= 128:
                    break
                if stop == start + 1:
                    raise ValueError("INCOMPATIBLE_ENCODING")
                stop = start + max(1, (stop - start) // 2)
            if len(spans) >= MAX_FRAGMENTS:
                raise ValueError("RESOURCE_LIMIT")
            spans.append((start, start + len(piece)))
            start = split
    return spans


def unit_dense(matrix, n, dim):
    matrix = checked_batch(matrix, n, dim)
    norms = np.linalg.norm(matrix, axis=1)
    if not np.isfinite(norms).all() or (norms == 0).any():
        raise ValueError("INCOMPATIBLE_ENCODING")
    return normalize(matrix)


def role_id(encoding_id, role):
    return digest([encoding_id, role, "verbatim-no-prefix-v1"])


class CandidateIndex:
    alternative_e5 = False

    def __init__(self, scope, docs, mode="lexical", encoder=None, *, whole=False):
        if mode not in MODES or mode != "lexical" and encoder is None:
            raise ValueError("NOT_RUN_MISSING_BACKEND" if encoder is None else "INVALID_MODE")
        self.scope, self.mode, self.encoder, self.whole = dict(scope), mode, encoder, whole
        self.total = len(docs)
        if self.total > 10000 or sum(len(d.text.encode()) for d in docs) > 4 * 1024**2:
            raise ValueError("RESOURCE_LIMIT")
        if len({d.version_id for d in docs}) != self.total:
            raise ValueError("DUPLICATE_VERSION")
        self.spans, texts = [], []
        self.manifest = dict(
            fragmentation="whole-pr05" if whole else SPEC,
            anchor_codepoints=240,
            encoding_span="anchor",
            offset_unit="unicode_code_point",
            tokenizer="none",
            channel_budget_reviews=CHANNEL_BUDGET,
            per_review_per_channel=2,
            rrf_k0=60,
            tie="score_desc_fragment_id_asc",
        )
        self.dense_id = None
        self.diagnostics = {}
        started = perf_counter()
        token_s = 0.0
        count = None
        if mode in {"dense", "hybrid"}:
            from reviewscope.embeddings.sentence_transformer import SentenceTransformerProvider

            if self.alternative_e5:
                from reviewscope.embeddings.rerank import E5

                if type(encoder) is not E5:
                    raise ValueError("INCOMPATIBLE_ENCODING")
            elif encoder.contract != SentenceTransformerProvider().contract:
                raise ValueError("INCOMPATIBLE_ENCODING")
            self.dense_id = encoder.contract.encoding_id

            @lru_cache(maxsize=8192)
            def count(text):
                nonlocal token_s
                tick = perf_counter()
                length = encoder.token_count(text)
                token_s += perf_counter() - tick
                return length

            self.manifest.update(
                dense=asdict(encoder.contract),
                tokenizer=self.dense_id,
                document_role=self.role_id("document"),
            )
        self.eligible = self.truncated = 0
        members = []
        for doc in sorted(docs, key=lambda d: d.version_id):
            text = doc.text
            sha = text_sha(text)
            members.append((doc.version_id, doc.content_sha256, sha))
            if not text.strip():
                continue
            self.eligible += 1
            if whole:
                bounds = [(0, len(text))]
                self.truncated += int(count is not None and count(text) > 128)
            else:
                bounds = anchors(text, count)
            if len(self.spans) + len(bounds) > MAX_FRAGMENTS:
                raise ValueError("RESOURCE_LIMIT")
            namespace = digest(
                [scope, members[-1], self.manifest["fragmentation"], self.manifest["tokenizer"]]
            )
            for i, (a, b) in enumerate(bounds):
                self.spans.append(
                    Span(
                        doc.version_id,
                        doc.content_sha256,
                        sha,
                        digest([namespace, a, b]),
                        a,
                        b,
                        bounds[max(0, i - 1)][0],
                        bounds[min(len(bounds) - 1, i + 1)][1],
                    )
                )
                texts.append(text[a:b])
        self.manifest.update(
            members_sha256=digest(members), order_sha256=digest([s.fragment_id for s in self.spans])
        )
        self.diagnostics["segmentation_s"] = perf_counter() - started - token_s
        self.diagnostics["corpus_tokenizer_s"] = token_s
        started = perf_counter()
        self.lexical = self.dense = None
        if whole:
            self.manifest["tie"] = "pr05-seed-42"
        if texts and mode in {"lexical", "hybrid"}:
            self.vectorizer = TfidfVectorizer(
                analyzer="char_wb", ngram_range=(3, 5), dtype=np.float32
            )
            self.lexical = normalize(self.vectorizer.fit_transform(texts))
            self.manifest.update(
                lexical="sklearn-char_wb-3-5-l2-float32",
                sklearn=version("scikit-learn"),
                vocabulary_idf_sha256=digest(
                    list(
                        zip(
                            self.vectorizer.get_feature_names_out().tolist(),
                            self.vectorizer.idf_.tolist(),
                            strict=True,
                        )
                    )
                ),
            )
        self.diagnostics["lexical_fit_s"] = perf_counter() - started
        started = perf_counter()
        if texts and self.dense_id:
            # The provider preserves input order and validates each float32 batch.
            unique = list(dict.fromkeys(texts))
            positions = {t: i for i, t in enumerate(unique)}
            self.dense = unit_dense(encoder.encode(unique), len(unique), encoder.contract.dim)[
                [positions[t] for t in texts]
            ]
            self.diagnostics.update(
                unique_encoded=len(unique), input_tokens=sum(count(t) for t in texts)
            )
        self.diagnostics["corpus_dense_s"] = perf_counter() - started
        self.diagnostics["matrix_bytes"] = sum(
            array.nbytes
            for array in (
                (self.lexical.data, self.lexical.indices, self.lexical.indptr)
                if self.lexical is not None
                else ()
            )
        ) + (self.dense.nbytes if self.dense is not None else 0)
        self.manifest["index_sha256"] = digest([scope, self.manifest])

    def role_id(self, role):
        if self.alternative_e5:
            return digest([self.dense_id, role, "passage: " if role == "document" else "query: "])
        return role_id(self.dense_id, role)

    def search(self, query, k, role="query_to_passage"):
        if (
            role not in ROLES
            or not query.strip()
            or len(query.encode()) > 2048
            or not 1 <= k <= CHANNEL_BUDGET
        ):
            raise ValueError("INVALID_QUERY")
        started = perf_counter()
        sparse_query = dense_query = None
        if self.lexical is not None:
            sparse_query = normalize(self.vectorizer.transform([query]))
        if self.dense_id:
            if self.encoder.contract.encoding_id != self.dense_id:
                raise ValueError("INCOMPATIBLE_ENCODING")
            count = self.encoder.query_count if self.alternative_e5 else self.encoder.token_count
            if count(query) > 128:
                raise ValueError("QUERY_TOKEN_LIMIT")
            if self.dense is not None:
                encode = self.encoder.encode_query if self.alternative_e5 else self.encoder.encode
                dense_query = unit_dense(encode([query]), 1, self.encoder.contract.dim)
        encoded_at = perf_counter()
        scores = {}
        if sparse_query is not None:
            scores["lexical"] = (self.lexical @ sparse_query.T).toarray().ravel()
        if dense_query is not None:
            scores["dense"] = (self.dense @ dense_query.T).ravel()
        channels = defaultdict(dict)
        for channel, values in scores.items():
            counts = Counter()

            def tie(i):
                return (
                    digest([42, self.spans[i].version_id])
                    if self.whole
                    else self.spans[i].fragment_id
                )

            order = sorted(range(len(values)), key=lambda i: (-values[i], tie(i)))
            for rank, i in enumerate(order, 1):
                if not self.whole and channel == "lexical" and values[i] <= 0:
                    break
                vid = self.spans[i].version_id
                if vid not in counts and len(counts) == CHANNEL_BUDGET or counts[vid] == 2:
                    continue
                counts[vid] += 1
                channels[i][channel] = dict(rank=rank, similarity=float(values[i]))

        ranked_at = perf_counter()

        def score(i):
            values = channels[i].values()
            return (
                sum(1 / (60 + c["rank"]) for c in values)
                if self.mode == "hybrid"
                else next(iter(values))["similarity"]
            )

        ranked = sorted(
            channels,
            key=lambda i: (
                -score(i),
                digest([42, self.spans[i].version_id]) if self.whole else self.spans[i].fragment_id,
            ),
        )
        selected = {}
        hits = []
        for rank, i in enumerate(ranked, 1):
            span = self.spans[i]
            if span.version_id not in selected and len(selected) == k:
                continue
            review_rank = selected.setdefault(span.version_id, len(selected) + 1)
            hits.append(
                dict(
                    **asdict(span),
                    rank=rank,
                    review_rank=review_rank,
                    score=score(i),
                    channels=channels[i],
                )
            )
        self.query_timings = dict(
            query_encoding_s=encoded_at - started,
            exact_search_and_channel_rank_s=ranked_at - encoded_at,
            fusion_and_selection_s=perf_counter() - ranked_at,
        )
        return dict(
            scope=self.scope,
            index=self.manifest,
            query_sha256=text_sha(query),
            role=role,
            query_role_id=self.role_id(role) if self.dense_id else None,
            mode=self.mode,
            k=k,
            reviewed_count=self.total,
            eligible_review_count=self.eligible,
            excluded_blank=self.total - self.eligible,
            fragment_count=len(self.spans),
            candidate_count=len(hits),
            retrieved_review_count=len(selected),
            candidates=hits,
            shortfall="eligible channel candidates exhausted" if len(selected) < k else None,
        )


class E5CandidateIndex(CandidateIndex):
    """Offline experimental namespace, separate from candidate-search-1."""

    alternative_e5 = True
