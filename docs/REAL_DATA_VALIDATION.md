# Real-Data Validation (Phase 15)

This document is the operational guide for measuring ReviewScope's **existing
production detectors** against human labels on a real dataset.

It is a *measurement baseline*, not a calibration tool. Nothing here changes
detectors, thresholds, weights or the main UI. If a detector performs poorly,
that is the finding to report — not a reason to retune anything during Phase 15.

## Contents

1. [Why / non-goals](#why--non-goals)
2. [Privacy and data handling](#privacy-and-data-handling)
3. [Pipeline overview](#pipeline-overview)
4. [Stage 1 — score and sample](#stage-1--score-and-sample)
5. [Stage 2 — blind annotation and finalization](#stage-2--blind-annotation-and-finalization)
6. [Stage 3 — report](#stage-3--report)
7. [Annotation schema](#annotation-schema)
8. [Evaluation vs challenge sampling](#evaluation-vs-challenge-sampling)
9. [The blindness protocol](#the-blindness-protocol)
10. [Finalization, fingerprint binding and overrides](#finalization-fingerprint-binding-and-overrides)
11. [How to read the metrics](#how-to-read-the-metrics)
12. [Limitations and leakage risk](#limitations-and-leakage-risk)
13. [Public synthetic example](#public-synthetic-example)

## Why / non-goals

**Why.** ReviewScope's templated, specificity and duplicate detectors are
heuristics with hand-set thresholds and weights. Before trusting them on real
traffic they must be *measured* against independent human judgment on data that
was never used to design them.

**Non-goals.**

- No detector, threshold or weight is changed here.
- No automatic "calibration" is fit to this data.
- No claim that the challenge sample estimates population performance.

## Privacy and data handling

Real datasets, human labels and the produced reports are **private**. Put all of
them under a gitignored directory, by convention:

```text
validation_data/private/
```

The repository's `.gitignore` excludes `validation_data/` in full, and also
excludes the common artifact names (`annotations.duckdb`, `labels.csv`,
`score_table.json`, `sample_selection.json`, `validation_report.md`,
`validation_report.json`) wherever they appear. Before you commit anything, run:

```bash
git status --short
git check-ignore -v validation_data/private/annotations.duckdb
```

Only the committed **synthetic** fixture under `tests/data/fixtures/` is public.

## Pipeline overview

```text
raw reviews ──▶ [1] score + sample ──▶ score_table.json      (private)
                                      sample_selection.json  (score-free)
                                      label_template.csv
                                              │
                                              ▼
                           [2] blind annotation app ──▶ annotations.duckdb
                                              │
                                      finalize (lock) + fingerprint
                                              │
                                              ▼
                           [3] validation_report.py ──▶ validation_report.md/.json
```

The script paths assume the repository root is the working directory.

## Stage 1 — score and sample

```bash
python scripts/validation_sample.py data/private/reviews.csv \
    --out-dir validation_data/private \
    --evaluation-n 120 \
    --challenge-high 24 --challenge-medium 24 \
    --challenge-low 24 --challenge-duplicate 24
```

This:

1. loads the dataset through the **production** ingestion adapters;
2. runs the **production** detectors over the *full* dataset
   (`validation.scoring.compute_reviewscope_outputs`) and persists the review
   rows + embedding cache to `validation_data/private/dataset.duckdb`;
3. deterministically samples an evaluation SRS plus a disjoint, score-stratified
   challenge sample;
4. writes `score_table.json`, `sample_selection.json` and `label_template.csv`.

`sample_selection.json` is deliberately **score-free** (only `sample_type`,
`sampling_stratum` and neutral context) so it can be handed to annotators.

Useful flags: `--seed` (reproducibility), `--no-embeddings` (tests only),
`--force-store` (rebuild a store whose fingerprint no longer matches).

## Stage 2 — blind annotation and finalization

Run the blind labeling app:

```bash
python -m streamlit run app_labeling.py
```

In the sidebar set the validation directory (default `validation_data`), the
annotator id, and the three paths (`sample_selection.json`, `dataset.duckdb`,
`annotations.duckdb`). Verdicts are upserted by `review_id`, so an interrupted
batch resumes exactly where it stopped.

When every selected review is annotated you can lock the batch either from the
app (**Finalize batch (lock labels)**) or from the CLI:

```bash
python scripts/validation_finalize.py \
    --annotation-store validation_data/private/annotations.duckdb \
    --scores validation_data/private/score_table.json \
    --annotator-id alice
```

Finalization records `status`, `finalized_at`, `annotator_id`,
`dataset_fingerprint` and `label_count`. After that, `save_label` refuses to
write unless the caller explicitly overrides, and every override archives the
superseded verdict in `annotation_revisions`.

## Stage 3 — report

```bash
python scripts/validation_report.py \
    --scores validation_data/private/score_table.json \
    --store validation_data/private/dataset.duckdb \
    --annotation-store validation_data/private/annotations.duckdb \
    --labels-out validation_data/private/labels.csv \
    --selection validation_data/private/sample_selection.json \
    --out-md validation_data/private/validation_report.md \
    --out-json validation_data/private/validation_report.json
```

Or pass `--labels labels.csv` directly if the verdicts were exported first. The
report always keeps the representative evaluation partition and the diagnostic
challenge partition apart.

## Annotation schema

Labels are a canonical CSV / DuckDB row with `LABEL_COLUMNS`:

| Column | Meaning | Values |
|---|---|---|
| `review_id` | review being judged | required |
| `templated_label` | participates in a reusable template family | `organic`, `templated`, `uncertain` |
| `template_group_id` | shared id for a template family | free text |
| `specificity_label` | how specific/informative the text is | `low`, `medium`, `high`, `uncertain` |
| `duplicate_label` | duplicate of another labeled review | `unique`, `duplicate`, `uncertain` |
| `duplicate_group_id` | shared id for one duplicate group | free text |
| `reviewer_notes` | freeform note | free text |
| `annotation_schema_version` | defaults to `1.0` | string |
| `annotator_id` | who recorded it | string |
| `labeled_at` | UTC ISO-8601 stamp | string |
| `sample_type` | `evaluation` or `challenge` | enum |
| `sampling_stratum` | `random` / `high` / `medium` / `low` / `duplicate` | string |

Invalid enum cells are dropped to `None` with a loader warning rather than
failing the import. Cross-field inconsistencies (a `duplicate` label with no
group id, a group id without a `duplicate` label) are reported as warnings.

## Evaluation vs challenge sampling

- **Evaluation** — a deterministic simple random sample (`random` stratum).
  Only this partition feeds the headline precision / recall / F1 / FPR / FNR
  and the specificity distributions.
- **Challenge** — a *disjoint* sample stratified as `high` (templated >= 65),
  `medium` (40–65), `low` (< 65) plus suspected `duplicate`-group members.
  It deliberately over-represents the detector's own hits, so its numbers are
  **diagnostic only** and are never quoted as population performance.

Because templated and duplicate signals are cohort-relative, the whole dataset
is always scored first; sampling only selects which reviews get labeled, never
which reviews are scored.

## The blindness protocol

The labeling app is blind by construction:

- it loads only review rows and the score-free selection file;
- it never loads, receives or renders any ReviewScope output — no templated
  score, no specificity score, no duplicate group id, no stratum label;
- the annotator sees the review text plus neutral context only.

This is what makes the labels usable as ground truth: anything that leaks a
detector preference would anchor the annotator. Keep it that way — do not add
score columns to the app, the selection file or the label template.

## Finalization, fingerprint binding and overrides

`AnnotationStore` tracks a batch lifecycle in a singleton `annotation_batch`
row:

- `OPEN` — normal annotation; verdicts may be written freely.
- `FINALIZED` — the batch is locked and bound to a `dataset_fingerprint`.

Rules enforced by the store:

- writing to a `FINALIZED` batch raises `BatchFinalizedError` unless
  `override=True`;
- an override archives the previous verdict (`annotation_revisions`) before
  replacing it, so provenance is never silently destroyed;
- re-finalizing requires `override=True`;
- a stored fingerprint can never be replaced by a *different* fingerprint
  (`FingerprintMismatchError`); `verify_fingerprint(expected)` asserts that a
  report is built against the dataset the labels were made for.

This is what stops a report from silently mixing labels with a mutated dataset.

## How to read the metrics

- **Templated.** `uncertain` human labels are excluded from the binary
  confusion and counted separately. Predicted positive = templated score
  `>= reporting threshold` (65 by default, the production cut).
- **Specificity.** Spearman rank correlation between the human ordinal label
  (low=0 / medium=1 / high=2) and the ReviewScope score, plus a descriptive
  cross-tab against production confidence bands (LOW < 40, 40 <= MEDIUM < 65,
  HIGH >= 65). No band mapping is invented; near/far disagreements are counted
  and far-band examples are listed.
- **Duplicate.** PRIMARY is pair-level precision/recall/F1 over labeled reviews.
  SECONDARY is a Jaccard >= 0.5 group match whose split/merge ambiguity is
  documented. The two are never blended.
- **Disagreements.** Each human-vs-detector disagreement lists the score, the
  detector's signals and counter-signals, and a text excerpt for audit.

The 0..100 threshold sweep is descriptive sensitivity analysis and **not**
calibration.

## Limitations and leakage risk

- This is a pre-calibration baseline. Judge the detectors, do not tune them.
- Templated and duplicate scores are cohort-relative and computed on the full
  dataset; they will differ on a differently-sized import.
- Specificity ordinal agreement is descriptive (Spearman) only.
- A challenge sample, when present, overstates detector performance by design.
- **Leakage:** validation data must stay out of any training/calibration/test
  loop. Fitting thresholds on the same labels used to report performance
  invalidates the measurement; keep a held-out split if you ever calibrate.

## Public synthetic example

A fully offline, synthetic example (no model downloads, no real data) lives at:

- [`docs/examples/real_data_validation_example.md`](examples/real_data_validation_example.md)
- [`docs/examples/real_data_validation_example.json`](examples/real_data_validation_example.json)

It is generated from the committed fixture
`tests/data/fixtures/example_reviews.csv` + `example_labels.csv` by:

```bash
python scripts/generate_example_report.py
```

The generator pins `generated_at` and uses `use_embeddings=False`, so it is
deterministic and safe to run anywhere. It is a documentation aid only.
