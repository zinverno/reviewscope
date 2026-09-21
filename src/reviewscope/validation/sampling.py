"""Deterministic sampling for validation label sets (Phase 15).

Two conceptually distinct sample regimes are produced:

* **evaluation** — a deterministic simple random sample (SRS) of the eligible
  indexed dataset. This is the ONLY sample allowed to drive headline
  (representative) precision/recall/F1/FPR/FNR.
* **challenge** — intentionally enriched with high/medium/low templated scores
  and suspected duplicate groups. Used ONLY for diagnostic/error analysis and
  must never be mixed into representative population metrics.

Sampling happens AFTER the full dataset has been scored, so it can never change
the scores: a review's ReviewScope output is cohort-dependent and must be
computed against the full dataset, never against the selected subset.
"""

from __future__ import annotations

import json
import random
from dataclasses import field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from reviewscope.models.review import NormalizedReview
from reviewscope.validation.models import SampleType
from reviewscope.validation.scoring import ReviewScoreTable


class SelectionEntry(BaseModel):
    """One selected review with its sample attribution.

    Deliberately contains NO ReviewScope scores. The selection file feeds the
    labeling template and the post-unblinding comparison mode; keeping scores
    out of it is a defence-in-depth measure for annotator blindness.
    """

    model_config = ConfigDict(extra="ignore")

    review_id: str
    sample_type: SampleType
    sampling_stratum: str
    place_id: str
    in_predicted_duplicate_group: bool = False


class Selection(BaseModel):
    """The full deterministic selection with reproducibility metadata."""

    model_config = ConfigDict(extra="ignore")

    fingerprint: str
    seed: int
    evaluation_count: int
    challenge_counts: dict[str, int] = field(default_factory=dict)
    entries: list[SelectionEntry] = field(default_factory=list)

    def by_id(self) -> dict[str, SelectionEntry]:
        return {entry.review_id: entry for entry in self.entries}

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                self.model_dump(),
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> Selection:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def sample_evaluation_ids(review_ids: list[str], n: int, rng: random.Random) -> list[str]:
    """Deterministic simple random sample over sorted ids."""
    ordered = sorted(review_ids)
    if n <= 0:
        return []
    k = min(n, len(ordered))
    return rng.sample(ordered, k)


def challenge_strata(table: ReviewScoreTable) -> dict[str, list[str]]:
    """Partition scored reviews into templated-score strata.

    Strata are mutually exclusive (a review lands in exactly one stratum):
    ``high`` (score >= 65), ``medium`` (40 <= score < 65), ``low`` (score < 40).
    Suspected duplicates are sampled separately from the members of detected
    duplicate groups.
    """
    high: list[str] = []
    medium: list[str] = []
    low: list[str] = []
    for output in table.outputs.values():
        if output.templated_value >= 65:
            high.append(output.review_id)
        elif output.templated_value >= 40:
            medium.append(output.review_id)
        else:
            low.append(output.review_id)

    duplicate_members: set[str] = set()
    for group in table.duplicate_groups:
        duplicate_members.update(group.review_ids)

    return {
        "high": high,
        "medium": medium,
        "low": low,
        "duplicate": sorted(duplicate_members),
    }


def sample_challenge_ids(
    table: ReviewScoreTable,
    *,
    n_high: int,
    n_medium: int,
    n_low: int,
    n_duplicate: int,
    rng: random.Random,
    exclude: set[str] | frozenset[str] = frozenset(),
) -> list[tuple[str, str]]:
    """Deterministic score/duplicate-stratified sample.

    Each returned pair is ``(review_id, stratum)``. Strata are drawn in the
    order high -> medium -> low -> duplicate; a review already chosen (or in
    ``exclude``) is skipped, so strata are disjoint and never overlap the
    evaluation sample.
    """
    strata = challenge_strata(table)
    chosen: set[str] = set(exclude)
    out: list[tuple[str, str]] = []
    for stratum, count in (
        ("high", n_high),
        ("medium", n_medium),
        ("low", n_low),
        ("duplicate", n_duplicate),
    ):
        pool = [rid for rid in strata[stratum] if rid not in chosen]
        selected = rng.sample(sorted(pool), min(count, len(pool)))
        for rid in selected:
            chosen.add(rid)
            out.append((rid, stratum))
    return out


def build_selection(
    reviews: list[NormalizedReview],
    table: ReviewScoreTable,
    *,
    evaluation_n: int,
    challenge_high: int,
    challenge_medium: int,
    challenge_low: int,
    challenge_duplicate: int,
    seed: int,
) -> Selection:
    """Create the full selection: an evaluation SRS plus a disjoint challenge
    strata sample, deterministically."""
    rng = random.Random(seed)
    all_ids = sorted(table.outputs)
    evaluation_ids = set(sample_evaluation_ids(all_ids, evaluation_n, rng))

    challenge_pairs = sample_challenge_ids(
        table,
        n_high=challenge_high,
        n_medium=challenge_medium,
        n_low=challenge_low,
        n_duplicate=challenge_duplicate,
        rng=rng,
        exclude=evaluation_ids,
    )

    duplicate_members = set()
    for group in table.duplicate_groups:
        duplicate_members.update(group.review_ids)

    entries: list[SelectionEntry] = []
    for review_id in all_ids:
        if review_id in evaluation_ids:
            entries.append(
                SelectionEntry(
                    review_id=review_id,
                    sample_type=SampleType.EVALUATION,
                    sampling_stratum="random",
                    place_id=table.outputs[review_id].place_id,
                    in_predicted_duplicate_group=review_id in duplicate_members,
                )
            )
    for review_id, stratum in challenge_pairs:
        entries.append(
            SelectionEntry(
                review_id=review_id,
                sample_type=SampleType.CHALLENGE,
                sampling_stratum=stratum,
                place_id=table.outputs[review_id].place_id,
                in_predicted_duplicate_group=review_id in duplicate_members,
            )
        )

    entries.sort(key=lambda entry: (entry.sample_type.value, entry.review_id))
    return Selection(
        fingerprint=table.fingerprint,
        seed=seed,
        evaluation_count=len(evaluation_ids),
        challenge_counts={
            "high": sum(1 for e in entries if e.sampling_stratum == "high"),
            "medium": sum(1 for e in entries if e.sampling_stratum == "medium"),
            "low": sum(1 for e in entries if e.sampling_stratum == "low"),
            "duplicate": sum(1 for e in entries if e.sampling_stratum == "duplicate"),
        },
        entries=entries,
    )
