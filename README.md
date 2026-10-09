# ReviewScope

Local analytics toolkit for review informativeness and coordinated-activity
signals. ReviewScope ingests a CSV/JSON dataset of Google-Maps-style reviews,
persists them in DuckDB, runs a multi-phase analysis pipeline and presents the
results in a Streamlit dashboard.

> **Weighted rating is an analytical model produced by ReviewScope — it is
> *not* an official platform rating.**

## Features

* **Ingestion** — CSV and JSON adapters with a normalization layer (§6).
* **Storage** — DuckDB store with a review table and an embeddings cache (§32).
* **Analysis pipeline** — burst detection, rating-anomaly detection, duplicate
  detection (exact / fuzzy / LSH / semantic), templated-text scoring, topic
  clustering, specificity, reviewer metrics, category experience, local
  familiarity and reviewer relevance (§14–§21).
* **Scoring** — per-review weight [0.25, 2.0] with documented penalties and a
  place-level coordinated activity score 0..100 with explainable signals and
  counter-signals (§14, §22, §23).
* **Weighted rating** — per-place raw vs. weighted average with an explanation
  of *why* they differ.
* **Keywords** — general, sentiment-split and emerging keywords (§9).
* **Streamlit UI** — place picker, Overview, Discover, Topics, Anomalies,
  Duplicates, Reviewers, Reviewed Places (map) and Data Quality pages
  (§26–§31).
* **Demo dataset** — deterministic 1 058-review generator with several
  documented manipulation injections (§7).
* **Dataset-level discovery (Phase 16)** — a `Discover` page that shows where
  the review evidence sits across every place: dataset totals, descriptive
  rankings (duplicate rate, repeated-family size, raw-vs-weighted rating delta,
  specificity, topic clusters, templated text, review cohorts, rating extremes)
  and a robust category comparison, with one click into any place's Overview.

## Architecture

```text
reviewscope/
├── app.py                          ← Streamlit entry point
├── src/reviewscope/
│   ├── analysis/                   ← detectors, scorers, engine
│   ├── discovery/                  ← dataset summary, rankings, category stats
│   ├── embeddings/                 ← sentence-transformer embeddings
│   ├── ingestion/                  ← CSV / JSON adapters
│   ├── models/                     ← NormalizedReview, ScoreResult
│   ├── storage/                    ← DuckDB store
│   └── ui/                         ← Streamlit page modules
├── scripts/generate_demo_data.py   ← demo generator
└── tests/                          ← pytest + AppTest smoke
```

## Installation

ReviewScope embeds review texts with a sentence-transformer model. Install the
CPU wheel of PyTorch first so the default `pip install` resolves the large
GPU build for nothing:

```bash
python -m venv .venv
source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -e .
```

## Generate demo dataset

```bash
python scripts/generate_demo_data.py
```

This creates `data/demo_reviews.csv` (1 058 reviews) and a DuckDB database
at `data/reviewscope.duckdb`.

## Run

```bash
streamlit run app.py
```

## Run tests

```bash
pytest
```

Ruff is used for linting:

```bash
ruff check src/ tests/ scripts/ app.py
```

## Data schema

Every review is a `NormalizedReview` with the following key fields:

| Field | Type | Description |
|---|---|---|
| `review_id` | str | unique id |
| `place_id` | str | place identifier |
| `place_name` | str | human-readable place name |
| `place_category` | str | business category |
| `reviewer_id` | str | reviewer identifier |
| `rating` | int (1–5) | review rating |
| `text` | str | review body text |
| `published_at` | str (ISO) | publication timestamp |
| `city` | str | reviewer city |
| `region` | str | reviewer region |
| `latitude` | float | place latitude |
| `longitude` | float | place longitude |

## Scoring methodology

### Coordinated activity score (§14)

A 0..100 composite of seven normalized signals: volume anomaly, rating
anomaly, semantic similarity, duplicate density, temporal density, template
similarity and reviewer overlap. Each signal is weighted; the sum determines
the score. Confidence: HIGH (≥ 65), MEDIUM (≥ 35), LOW.

### Per-review weight (§22)

Each review is weighted in `[0.25, 2.0]` from the caller-provided quality
signals and the graded manipulation probabilities. Inputs are normalized to
0..1 first:

- `specificity` (0..100) → `specificity / 100`
- `category_experience` (0..100) → `category_experience / 100`
- `reviewer_relevance` (0..100) → `reviewer_relevance / 100`
- `recency` is already normalized to 0..1

```text
quality =
    0.35 * specificity
  + 0.20 * category_experience
  + 0.25 * reviewer_relevance
  + 0.20 * recency

neutral_quality = 0.50
excess = max(0, quality - neutral_quality)

rise_factor = 2.0

text_reuse  = 0.30 * max(duplicate_probability, templated_probability)
coord_resid = max(0, coordinated_probability - text_reuse)

penalty =
    0.30 * duplicate_probability
  + 0.25 * templated_probability
  + 0.20 * coord_resid

raw_weight = 1.0 + rise_factor * excess - penalty
weight = clamp(raw_weight, 0.25, 2.0)
```

- Duplicate and templated evidence is charged directly, once each.
- The three penalties are **not** independent: `coordinated_probability`
  already embeds the same text-reuse evidence (its
  `CoordinatedConfig.review_probability_components["duplicate_templated"]`
  share, 0.30 by default). That share is subtracted back out as
  `text_reuse`, so `coord_resid` — and therefore `penalty_coordinated` —
  carries only coordinated evidence of its own: event participation, peer
  semantics and temporal density. Text reuse is never charged twice.
- Neutral reviews (quality ≈ 0.50, no penalties) stay around weight 1.0.
- High-quality reviews (specific, category-experienced, relevant, fresh)
  elevate `quality` above the neutral threshold and can exceed 1.0.
- The final value is always bounded to `[0.25, 2.0]`; with bounded inputs the
  reachable penalty range is `[0, 0.69]`, i.e. weights in `[0.31, 2.0]`.
  `0.25` remains configured as a safety clamp for malformed inputs, not as a
  reachable all-penalties result.

### Weighted rating (§23)

Per-place average where each review contributes `rating × weight / Σweights`.
The delta from the raw average and its direction are always explained.

### Repeated-text families (§29)

Duplicate detection first compares reviews **pairwise**: exact text, fuzzy
(RapidFuzz), near-duplicate (character n-gram TF-IDF) and semantic similarity
(cosine ≥ 0.88 on the embedding model). Each passing pair becomes an edge.
A `DuplicateGroup` — shown in the UI as a **repeated-text family** — is the
*connected component* of that edge graph, not a set of reviews that are all
similar to each other.

> Reviews are grouped when they are connected through one or more strong
> text-similarity relationships. Not every pair inside a larger family must
> directly pass the similarity threshold.

Consequences the UI states explicitly:

* **Family size describes connectivity, not mutual similarity.** A family of
  5 needs only a chain of 4 links; the pair at the two ends may be unrelated.
  The Duplicates page reports `N direct links of M possible pairs` and, when
  fewer than half of all pairs link, adds *Contains transitive connections*.
* **Evidence is per link.** Each member lists its direct links and their
  detection kind (`exact` / `fuzzy` / `near` / `semantic`), so a missing line
  between two members is visible rather than implied away.
* **Rating alignment is separate context.** Star-rating agreement is shown as
  `Rating context (separate from text matching)` and never gates, ranks or
  re-weights membership — rating alignment never decides who is in a family.
* **Counting rules are labelled.** `Identical-text counts are review counts;
  fuzzy/near/semantic counts are link counts`, and `Identical-text links` is
  reported separately from `Identical-text reviews`.

## Dataset discovery (Phase 16)

The `Discover` page aggregates the **production** per-place analysis into a
dataset-level view. It introduces no new detector, threshold, weight or
formula — every number is a projection of the same place-level outputs the
Overview already shows.

* **Descriptive, never accusatory** — cards are worded "highest duplicate
  rate", "largest repeated-text families", "lowest specificity". A place can top
  a "most 5★ reviews" list and a "lowest specificity" list at the same time.
* **Missing evidence is `N/A`, not zero** — the page first states what the
  dataset cannot support (no publication timestamps, no reviewer history, no
  coordinates, no ratings). Coordinated activity is *not ranked* without
  timestamps, because its strongest components are temporal; the per-place
  score stays readable on that place's Overview.
* **Failed analysis is visible** — a place whose analysis fails is listed with
  `N/A` evidence and counted in a warning, never as zeros.
* **Same metric, same meaning** — Discover reuses the production definitions
  rather than re-deriving them, and where two production surfaces genuinely use
  different floors it says so:
  * *Duplicate rate* = reviews in repeated-text families of **3+** — the exact
    Overview definition. The Duplicates page's *Share of place reviews* counts
    **all** repeated-text families including pairs, so for a place with a family
    of 6 and a pair the two read 15.0% and 20.0%. They answer different
    questions.
  * *Families (2+)* = every detected repeated-text family (pairs included),
    matching the Duplicates page's *Repeated-text families*. The column name
    states its floor so it is not read as the count behind the 3+ rate. A
    repeated-text family is a **connected component**, so the column counts
    families, not pairwise-similar sets.
  * *Raw − weighted* = the difference of the two ratings printed in the same
    row (the same derivation as the Overview verdict line). The finer-grained
    production delta, computed before raw/weighted are rounded, stays on the
    place Overview under technical details.
  * *Categories* are the dataset's own `place_category` values. On corpora built
    from a generation-side taxonomy (e.g. the exploratory Yandex corpus, whose
    manifest groups 10 target categories but stores the organisation's primary
    rubric) the count reflects the stored values, not the generation buckets.
* **Caching** — the summary is keyed by the resolved database path, size,
  modification time and embedding model. It is cached in-process and in a JSON sidecar under
  `RS_CACHE_DIR` (or `XDG_CACHE_HOME`) in `reviewscope/discovery-cache/`, so a
  10k-review corpus costs one slow pass and then loads in well under a second.
  Set `RS_DISCOVERY_DISK_CACHE=0` to keep the cache in memory only. Sidecars
  contain aggregates only, never review text, and are rewritten when the
  database changes.

## Real-data validation (Phase 15)

The detector outputs can be measured against independent human labels on a real
dataset through a separate, score-blind workflow:

1. `scripts/validation_sample.py` scores the full dataset with the **production**
   detectors and builds a deterministic evaluation + challenge selection;
2. `app_labeling.py` is a blind annotation app that never loads or shows any
   detector output;
3. `scripts/validation_finalize.py` locks the batch and binds it to the dataset
   fingerprint;
4. `scripts/validation_report.py` joins scores + labels + sampling and writes
   the report.

It is a **measurement baseline, not a calibration tool** — no detector,
threshold or weight is changed. Validation data is private: keep it under the
gitignored `validation_data/` directory.

See [docs/REAL_DATA_VALIDATION.md](docs/REAL_DATA_VALIDATION.md) for the full
protocol, and [the public synthetic example](docs/examples/real_data_validation_example.md)
for an offline, model-free sample report.

## Limitations

* A **coordinated activity anomaly is not proof of fraud**. It is a
  statistical signal indicating a burst of activity that deviates from the
  baseline.
* A **synthetic-like templated score is not proof of AI-generated text**.
  It flags structural and phrasing similarity against a review's peer group.
* A **publication date is not a verified visit date**. The date comes from the
  data source and may not match the actual visit.
* **Weighted rating is an analytical model produced by ReviewScope**, not an
  official platform rating or endorsement.
* **A Discover ranking position is not a finding.** The cards rank observed
  evidence — a top position means "this measurement is high here", nothing more.
  A dataset-wide pass also inherits every per-place failure: places that fail
  analysis are shown as `N/A` rows rather than being silently dropped.


## Public demo (Community Cloud)

- Opt-in via environment/secret: `RS_PUBLIC_DEMO=1` or Streamlit secret `public_demo = "1"`.
- Runs against the committed packaged dataset `demo/reviewscope_demo.duckdb` with precomputed embeddings (no `sentence-transformers`/PyTorch required at runtime).
- Read-only: fixed dataset, no path input, annotation tooling disabled, discovery disk cache forced off. All review text and reviewer identifiers are escaped before rendering.
- See `docs/DEPLOYMENT.md` for exact Streamlit Community Cloud configuration (Python 3.12, `requirements.txt`, secrets).
