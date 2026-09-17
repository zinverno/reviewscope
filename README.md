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
* **Streamlit UI** — place picker, Overview, Topics, Anomalies, Duplicates,
  Reviewers, Reviewed Places (map) and Data Quality pages (§26–§31).
* **Demo dataset** — deterministic 1 058-review generator with several
  documented manipulation injections (§7).

## Architecture

```text
reviewscope/
├── app.py                          ← Streamlit entry point
├── src/reviewscope/
│   ├── analysis/                   ← detectors, scorers, engine
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

penalty =
    0.30 * duplicate_probability
  + 0.25 * templated_probability
  + 0.20 * coordinated_probability

raw_weight = 1.0 + rise_factor * excess - penalty
weight = clamp(raw_weight, 0.25, 2.0)
```

- Neutral reviews (quality ≈ 0.50, no penalties) stay around weight 1.0.
- High-quality reviews (specific, category-experienced, relevant, fresh)
  elevate `quality` above the neutral threshold and can exceed 1.0.
- Duplicate / templated / coordinated signals reduce the weight.
- The final value is always bounded to `[0.25, 2.0]`.

### Weighted rating (§23)

Per-place average where each review contributes `rating × weight / Σweights`.
The delta from the raw average and its direction are always explained.

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
