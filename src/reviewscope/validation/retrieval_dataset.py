"""Frozen query, document and judgment bundle for offline candidate retrieval."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Split = Literal["train", "dev", "test", "future", "novel_place"]
Mode = Literal["query_to_passage", "text_to_text"]
Method = Literal["tfidf", "minilm"]


def digest(value: object) -> str:
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Manifest(Record):
    schema_version: Literal["retrieval-v1"]
    dataset_id: Text
    kind: Literal["synthetic", "private"]
    provenance: Text
    permission_ref: Text
    annotation_source: Literal["authored_smoke", "independent_human"]
    annotation_version: Text
    labeled_on: date
    split_protocol: Text
    methods: list[Method] = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class Query(Record):
    query_id: Text
    query_text: Text
    problem_id: Text
    definition_version: Text
    definition: Text
    mode: Mode
    exemplar_family: Text | None = None


class Document(Record):
    review_id: Text
    text: Text
    source: Text
    language: Text | None = None
    place_id: Text | None = None
    period: date | None = None
    family_id: Text
    split: Split


class Judgment(Record):
    query_id: Text
    review_id: Text
    relevance: Annotated[StrictInt, Field(ge=0, le=2)] | None
    stance: Literal["complaint", "denial", "mixed", "unclear", "not_applicable"] | None
    evidence_span: Text | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.relevance is None:
            if self.stance is not None or self.evidence_span is not None:
                raise ValueError("unknown relevance requires null stance and evidence")
        elif self.stance is None or (self.relevance == 2) != (
            self.stance in ("complaint", "mixed")
        ):
            raise ValueError("grade 2 requires complaint/mixed; every known grade needs a stance")
        elif self.relevance == 0 and self.stance != "not_applicable":
            raise ValueError("grade 0 requires not_applicable")
        return self


class Dataset(Record):
    manifest: Manifest
    queries: list[Query] = Field(min_length=1)
    documents: list[Document] = Field(min_length=1)
    judgments: list[Judgment] = Field(min_length=1)

    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"manifest": {"fingerprint"}}))

    @model_validator(mode="after")
    def consistent(self) -> Self:
        queries = {q.query_id for q in self.queries}
        docs = {d.review_id: d for d in self.documents}
        if len(queries) != len(self.queries) or len(docs) != len(self.documents):
            raise ValueError("duplicate query_id or review_id")
        m = self.manifest
        if "tfidf" not in m.methods or len(set(m.methods)) != len(m.methods):
            raise ValueError("method plan must include tfidf and contain unique methods")
        if (m.kind == "private") != (m.annotation_source == "independent_human"):
            raise ValueError("annotation_source must match the dataset kind")
        if m.kind == "private" and not any(d.split == "test" for d in self.documents):
            raise ValueError("private protocol requires a frozen test split")

        owners: dict[tuple[str, str], str] = {}
        places: set[str] = set()
        novel: set[str] = set()
        future_dates = []
        previous_dates = []
        for d in self.documents:
            exact = " ".join(d.text.casefold().split())
            for key in (("family", d.family_id), ("text", exact)):
                if owners.setdefault(key, d.split) != d.split:
                    raise ValueError("duplicate family or normalized text crosses splits")
            if d.split == "novel_place":
                novel.add(d.place_id or "")
            elif d.split == "future":
                future_dates.append(d.period)
            else:
                places.add(d.place_id or "")
                previous_dates.append(d.period)
        if novel and (not places or "" in novel | places or novel & places):
            raise ValueError("novel_place requires known, disjoint place IDs")
        if future_dates and (
            not previous_dates
            or None in future_dates + previous_dates
            or min(future_dates) <= max(previous_dates)
        ):
            raise ValueError("future requires known dates strictly after train/dev/test")

        definitions = {}
        for q in self.queries:
            key = (q.problem_id, q.definition_version)
            if definitions.setdefault(key, q.definition) != q.definition:
                raise ValueError("conflicting definition for problem_id/version")
            if (q.mode == "text_to_text") != bool(q.exemplar_family):
                raise ValueError("text_to_text requires an external exemplar family")
            if q.exemplar_family and (
                ("family", q.exemplar_family) in owners
                or ("text", " ".join(q.query_text.casefold().split())) in owners
            ):
                raise ValueError("exemplar overlaps the search corpus")

        pairs = set()
        for j in self.judgments:
            pair = (j.query_id, j.review_id)
            if pair in pairs or j.query_id not in queries or j.review_id not in docs:
                raise ValueError("duplicate judgment or unresolved query/document reference")
            if j.evidence_span and j.evidence_span not in docs[j.review_id].text:
                raise ValueError("evidence_span must be an exact substring of document text")
            pairs.add(pair)
        if all(j.relevance is None for j in self.judgments):
            raise ValueError("no known relevance judgments")
        return self


def load_dataset(path: Path, *, check_fingerprint: bool = True) -> Dataset:
    data = Dataset.model_validate_json(path.read_bytes())
    if check_fingerprint and data.fingerprint() != data.manifest.fingerprint:
        raise ValueError("dataset fingerprint mismatch")
    return data
