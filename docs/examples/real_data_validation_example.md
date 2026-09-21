# ReviewScope Real Data Validation

- Generated: `2026-03-26T00:00:00+00:00`
- Annotation schema version: `1.0`
- Templated reporting threshold: `65` (reporting choice; sweep is descriptive only)

> This report is a MEASUREMENT of the existing production ReviewScope detectors against human labels. No thresholds, weights or detector algorithms were modified to produce it. The templated decision threshold shown below is a reporting choice; the 0..100 sweep is descriptive sensitivity analysis and is NOT an optimised threshold.

## Dataset

| Field | Value |
|---|---|
| Reviews loaded | 22 |
| Distinct places | 4 |
| Embedding model | sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 |
| Dataset fingerprint | v1|22|4|a92e2e8945f168ce|9d5e8d8a7c4251d0|sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 |
Metric definitions:

- **templated** — Confusion matrix, precision = TP/(TP+FP), recall = TP/(TP+FN), F1, FPR = FP/(FP+TN), FNR = FN/(FN+TP) at the reporting threshold (default 65, matching the production templated signal cut). 'uncertain' human labels are excluded from binary metrics and counted separately.
- **specificity** — Score distributions by human label; Spearman rank correlation between the human ordinal label (low=0/medium=1/high=2) and the ReviewScope specificity score; descriptive cross-tab with the production confidence bands (LOW<40, 40<=MEDIUM<65, HIGH>=65). No band mapping is invented.
- **duplicate** — PRIMARY: pair-level precision/recall/F1 (GT pairs = pairs of reviews sharing a human duplicate_group_id; predicted pairs = pairs inside ReviewScope duplicate groups, restricted to labeled reviews). SECONDARY (descriptive): Jaccard>=0.5 group matching whose split/merge ambiguity is documented. The two are never blended.

## Label coverage

| Field | Value |
|---|---|
| Label records | 22 |
| Matched to dataset | 22 |
| Unmatched | 0 |
| Template groups annotated | 8 |

By label:

- **templated_label:** organic=11, templated=8, uncertain=3
- **template_group_id:** set=8
- **specificity_label:** high=4, low=10, medium=7, uncertain=1
- **duplicate_label:** duplicate=13, uncertain=1, unique=8
- **sample_type:** challenge=12, evaluation=10
- **sampling_stratum:** duplicate=4, high=3, low=5, random=10

## Representative Evaluation Metrics

Labeled reviews in this partition: **10**

> Representative: deterministic simple random sample; headline population metrics are reported from this partition only.

### Templated Detection

| Field | Value |
|---|---|
| TP | 3 |
| FP | 0 |
| TN | 6 |
| FN | 1 |
| Precision | 1.0000 |
| Recall | 0.7500 |
| F1 | 0.8571 |
| False-positive rate | 0.0000 |
| False-negative rate | 0.2500 |
| Reference organic | 6 |
| Reference templated | 4 |
| Excluded (uncertain/missing) | 0 |

Score distributions:

- **Human organic** n=6, min=7.0, p25=14.2, median=15.6, mean=17.38, p75=25.1, p95=28.1, max=28.1
- **Human templated** n=4, min=14.2, p25=96.7, median=96.7, mean=76.08, p75=96.7, p95=96.7, max=96.7

Threshold sweep (descriptive sensitivity analysis, not calibration):

| Threshold | Precision | Recall | F1 |
|---|---|---|---|
| 0 | 0.4000 | 1.0000 | 0.5714 |
| 5 | 0.4000 | 1.0000 | 0.5714 |
| 10 | 0.4444 | 1.0000 | 0.6154 |
| 15 | 0.5000 | 0.7500 | 0.6000 |
| 20 | 0.6000 | 0.7500 | 0.6667 |
| 25 | 0.6000 | 0.7500 | 0.6667 |
| 30 | 1.0000 | 0.7500 | 0.8571 |
| 35 | 1.0000 | 0.7500 | 0.8571 |
| 40 | 1.0000 | 0.7500 | 0.8571 |
| 45 | 1.0000 | 0.7500 | 0.8571 |
| 50 | 1.0000 | 0.7500 | 0.8571 |
| 55 | 1.0000 | 0.7500 | 0.8571 |
| 60 | 1.0000 | 0.7500 | 0.8571 |
| 65 | 1.0000 | 0.7500 | 0.8571 |
| 70 | 1.0000 | 0.7500 | 0.8571 |
| 75 | 1.0000 | 0.7500 | 0.8571 |
| 80 | 1.0000 | 0.7500 | 0.8571 |
| 85 | 1.0000 | 0.7500 | 0.8571 |
| 90 | 1.0000 | 0.7500 | 0.8571 |
| 95 | 1.0000 | 0.7500 | 0.8571 |
| 100 | n/a | 0.0000 | 0.0000 |

### Specificity

- **Spearman** (human ordinal low/medium/high vs ReviewScope score): 0.6095 (n=10)
- Near-band disagreements: **1**, far-band disagreements: **2**
  - far-band example: human `high` vs production `LOW` band (n=1)
  - far-band example: human `low` vs production `HIGH` band (n=1)

Distributions by human label:

- **high** n=3, min=27.5, p25=27.5, median=82.0, mean=67.17, p75=92.0, p95=92.0, max=92.0
- **low** n=5, min=12.5, p25=12.5, median=12.5, mean=27.2, p75=12.5, p95=86.0, max=86.0
- **medium** n=2, min=60.0, p25=60.0, median=82.0, mean=71.0, p75=82.0, p95=82.0, max=82.0

Cross-tab (human label × production confidence band):

| Human label | LOW | MEDIUM | HIGH |
|---|---|---|---|
| low | 4 | 0 | 1 |
| medium | 0 | 1 | 1 |
| high | 1 | 0 | 2 |

### Duplicate Detection

**Metric definitions** (also in `raw_metrics`):

- Primary: pair-level: precision = TP/(TP+FP), recall = TP/(TP+FN), where TP/FP/FN are unordered review pairs inside human duplicate groups (ground truth) vs pairs inside ReviewScope duplicate groups (predicted), restricted to labeled reviews.
- Secondary: group-level Jaccard>=0.5 match between a human group and a ReviewScope group. Descriptive only: split/merge ambiguity makes group matching inexact; it is never blended with pair-level numbers.

| Field | Value |
|---|---|
| Known duplicate pairs recovered (TP) | 3 |
| Known duplicate pairs total | 4 |
| Missed duplicate pairs (FN) | 1 |
| False-positive duplicate pairs (FP) | 0 |
| Pair precision | 1.0000 |
| Pair recall | 0.7500 |
| Pair F1 | 0.8571 |

Group-level (Jaccard>=0.5, descriptive only):

| Field | Value |
|---|---|
| Human groups | 3 |
| Predicted groups | 3 |
| Recovered human groups | 1 |
| Missed human groups | 2 |
| Matched predicted groups | 3 |
| Unmatched predicted groups | 0 |


## Challenge / Error Analysis (diagnostic only, NOT representative)

Labeled reviews in this partition: **12**

> Diagnostic only: score/duplicate-enriched challenge sample. These numbers are NOT representative of population performance and MUST NOT be quoted as headline precision/recall/F1/FPR/FNR.

### Templated Detection

| Field | Value |
|---|---|
| TP | 3 |
| FP | 1 |
| TN | 4 |
| FN | 1 |
| Precision | 0.7500 |
| Recall | 0.7500 |
| F1 | 0.7500 |
| False-positive rate | 0.2000 |
| False-negative rate | 0.2500 |
| Reference organic | 5 |
| Reference templated | 4 |
| Excluded (uncertain/missing) | 3 |

Score distributions:

- **Human organic** n=5, min=11.0, p25=25.1, median=26.0, mean=37.38, p75=28.1, p95=96.7, max=96.7
- **Human templated** n=4, min=14.3, p25=96.7, median=96.7, mean=76.1, p75=96.7, p95=96.7, max=96.7

Threshold sweep (descriptive sensitivity analysis, not calibration):

| Threshold | Precision | Recall | F1 |
|---|---|---|---|
| 0 | 0.4444 | 1.0000 | 0.6154 |
| 5 | 0.4444 | 1.0000 | 0.6154 |
| 10 | 0.4444 | 1.0000 | 0.6154 |
| 15 | 0.4286 | 0.7500 | 0.5455 |
| 20 | 0.4286 | 0.7500 | 0.5455 |
| 25 | 0.4286 | 0.7500 | 0.5455 |
| 30 | 0.7500 | 0.7500 | 0.7500 |
| 35 | 0.7500 | 0.7500 | 0.7500 |
| 40 | 0.7500 | 0.7500 | 0.7500 |
| 45 | 0.7500 | 0.7500 | 0.7500 |
| 50 | 0.7500 | 0.7500 | 0.7500 |
| 55 | 0.7500 | 0.7500 | 0.7500 |
| 60 | 0.7500 | 0.7500 | 0.7500 |
| 65 | 0.7500 | 0.7500 | 0.7500 |
| 70 | 0.7500 | 0.7500 | 0.7500 |
| 75 | 0.7500 | 0.7500 | 0.7500 |
| 80 | 0.7500 | 0.7500 | 0.7500 |
| 85 | 0.7500 | 0.7500 | 0.7500 |
| 90 | 0.7500 | 0.7500 | 0.7500 |
| 95 | 0.7500 | 0.7500 | 0.7500 |
| 100 | n/a | 0.0000 | 0.0000 |

### Specificity

- **Spearman** (human ordinal low/medium/high vs ReviewScope score): 0.9535 (n=11)
- Near-band disagreements: **3**, far-band disagreements: **0**

Distributions by human label:

- **high** n=1, min=100.0, p25=100.0, median=100.0, mean=100.0, p75=100.0, p95=100.0, max=100.0
- **low** n=5, min=12.5, p25=12.5, median=12.5, mean=12.5, p75=12.5, p95=12.5, max=12.5
- **medium** n=5, min=27.5, p25=27.5, median=33.0, mean=41.6, p75=60.0, p95=60.0, max=60.0
- **uncertain** n=1, min=60.0, p25=60.0, median=60.0, mean=60.0, p75=60.0, p95=60.0, max=60.0

Cross-tab (human label × production confidence band):

| Human label | LOW | MEDIUM | HIGH |
|---|---|---|---|
| low | 5 | 0 | 0 |
| medium | 3 | 2 | 0 |
| high | 0 | 0 | 1 |

### Duplicate Detection

**Metric definitions** (also in `raw_metrics`):

- Primary: pair-level: precision = TP/(TP+FP), recall = TP/(TP+FN), where TP/FP/FN are unordered review pairs inside human duplicate groups (ground truth) vs pairs inside ReviewScope duplicate groups (predicted), restricted to labeled reviews.
- Secondary: group-level Jaccard>=0.5 match between a human group and a ReviewScope group. Descriptive only: split/merge ambiguity makes group matching inexact; it is never blended with pair-level numbers.

| Field | Value |
|---|---|
| Known duplicate pairs recovered (TP) | 3 |
| Known duplicate pairs total | 4 |
| Missed duplicate pairs (FN) | 1 |
| False-positive duplicate pairs (FP) | 4 |
| Pair precision | 0.4286 |
| Pair recall | 0.7500 |
| Pair F1 | 0.5455 |

Group-level (Jaccard>=0.5, descriptive only):

| Field | Value |
|---|---|
| Human groups | 4 |
| Predicted groups | 3 |
| Recovered human groups | 1 |
| Missed human groups | 3 |
| Matched predicted groups | 3 |
| Unmatched predicted groups | 0 |


## Unattributed labels

- Count: **0**
- Labels without a sample_type and not present in the supplied selection file. Excluded from both representative evaluation and challenge diagnostics.

## Disagreements

Each row: review_id, human label, ReviewScope result, ReviewScope score, signals, counter-signals, text (truncated), notes.

### duplicate_false_positive — `c07`

| Field | Value |
|---|---|
| Human label | unique |
| ReviewScope result | duplicate group p-coffee|g1 |
| ReviewScope score | 7 |
| Sample | challenge |
| Stratum | duplicate |
- **Text:** Отличное место, всё понравилось, рекомендую всем!
- **Notes:** Reads personal to me despite similarity.

### duplicate_false_positive — `r03`

| Field | Value |
|---|---|
| Human label | unique |
| ReviewScope result | duplicate group p-rest|g1 |
| ReviewScope score | 3 |
| Sample | challenge |
| Stratum | duplicate |
- **Text:** Вкусно, быстро, недорого. Берём бизнес-ланч почти каждый день!

### duplicate_missed_group — `r04`

| Field | Value |
|---|---|
| Human label | duplicate |
| ReviewScope result | unique |
| ReviewScope score | 0.0 |
| Sample | evaluation |
| Stratum | random |
- **Text:** Паста карбонара с гуанчиале, соус не пересушен. Порция большая, взяли на двоих с бокалом кьянти за 1900 рублей.
- **Notes:** My partner's review is nearly the same.

### duplicate_missed_group — `n01`

| Field | Value |
|---|---|
| Human label | duplicate |
| ReviewScope result | unique |
| ReviewScope score | 0.0 |
| Sample | challenge |
| Stratum | duplicate |
- **Text:** Очень вкусная шаурма, готовят быстро, соус фирменный.
- **Notes:** Same wording with small changes.

### duplicate_missed_group — `n02`

| Field | Value |
|---|---|
| Human label | duplicate |
| ReviewScope result | unique |
| ReviewScope score | 0.0 |
| Sample | challenge |
| Stratum | low |
- **Text:** Шаурма вкусная, делают быстро, соус особенный.
- **Notes:** Same wording with small changes.

### specificity_far_human_high — `h02`

| Field | Value |
|---|---|
| Human label | high |
| ReviewScope result | LOW specificity |
| ReviewScope score | 27.5 |
| Sample | evaluation |
| Stratum | random |
- **Counter-signals:** no numeric detail, low entity density, very short text
- **Text:** Отличный отель, чисто и уютно.

### specificity_far_human_low — `c08`

| Field | Value |
|---|---|
| Human label | low |
| ReviewScope result | HIGH specificity |
| ReviewScope score | 86.0 |
| Sample | evaluation |
| Stratum | random |
- **Signals:** contains concrete numbers/time references, contains specific long-word entities, identifies involved staff/persons, narrative event description
- **Text:** Взяла фильтр Эфиопия Гуджи на V60, бариста Аня рассказала про лот и дескрипторы. Кислотность цитрусовая, послевкусие чайное, 300 мл за 350 рублей.
- **Notes:** Very detailed but I still think it is part of a paid campaign.

### templated_false_negative — `c08`

| Field | Value |
|---|---|
| Human label | templated |
| ReviewScope result | organic |
| ReviewScope score | 14.2 |
| Sample | evaluation |
| Stratum | random |
- **Signals:** stylistically uniform with group, reviews published in narrow time window
- **Counter-signals:** textually distinct from peers, high unique detail density
- **Text:** Взяла фильтр Эфиопия Гуджи на V60, бариста Аня рассказала про лот и дескрипторы. Кислотность цитрусовая, послевкусие чайное, 300 мл за 350 рублей.
- **Notes:** Very detailed but I still think it is part of a paid campaign.

### templated_false_negative — `r02`

| Field | Value |
|---|---|
| Human label | templated |
| ReviewScope result | organic |
| ReviewScope score | 14.3 |
| Sample | challenge |
| Stratum | low |
- **Signals:** stylistically uniform with group, reviews published in narrow time window
- **Counter-signals:** textually distinct from peers, high unique detail density
- **Text:** Вкусно, быстро, недорого. Берём бизнес-ланч почти каждый день.
- **Notes:** Repeated short lunch review.

### templated_false_positive — `c07`

| Field | Value |
|---|---|
| Human label | organic |
| ReviewScope result | templated |
| ReviewScope score | 96.7 |
| Sample | challenge |
| Stratum | duplicate |
- **Signals:** high fraction of near-identical peer reviews, repeated generic phrases across reviews, high structural similarity with peers, low specificity (generic language), few unique details vs the review cohort, stylistically uniform with group, reviews published in narrow time window
- **Text:** Отличное место, всё понравилось, рекомендую всем!
- **Notes:** Reads personal to me despite similarity.


## Score Distributions

> Aggregated over all labeled reviews. If the challenge sample is present this mix is intentionally score-biased and must not be read as a population distribution.

- **Templated score, human-organic reviews** n=11, min=7.0, p25=14.2, median=25.1, mean=26.47, p75=28.1, p95=96.7, max=96.7
- **Templated score, human-templated reviews** n=8, min=14.2, p25=96.7, median=96.7, mean=76.09, p75=96.7, p95=96.7, max=96.7
- **Specificity score, human `high`** n=4, min=27.5, p25=82.0, median=92.0, mean=75.38, p75=100.0, p95=100.0, max=100.0
- **Specificity score, human `low`** n=10, min=12.5, p25=12.5, median=12.5, mean=19.85, p75=12.5, p95=86.0, max=86.0
- **Specificity score, human `medium`** n=7, min=27.5, p25=27.5, median=60.0, mean=50.0, p75=60.0, p95=82.0, max=82.0
- **Specificity score, human `uncertain`** n=1, min=60.0, p25=60.0, median=60.0, mean=60.0, p75=60.0, p95=60.0, max=60.0

## Uncertain Labels

- templated_uncertain: **3**
- specificity_uncertain: **1**
- duplicate_uncertain: **1**
- Uncertain labels are always reported with their own count and are never forced into a binary bucket for headline metrics.

## Limitations

- This is a measurement baseline, produced before any calibration. If a detector performs poorly on real data, that is the finding to report, not a reason to retune thresholds during Phase 15.
- Templated and duplicate scores are cohort-relative: they depend on the reviews of the same place in the FULL dataset. Scores are always computed on the full imported dataset and never recomputed on a sample; sampling only selects which reviews get labeled.
- Threshold fitting on this dataset would leak into future evaluations: validation data must stay out of any calibration, training or test-tuning loop (see docs/REAL_DATA_VALIDATION.md for the train/calibration/test leakage risk).
- Specificity ordinal agreement is descriptive (Spearman) only; no scientifically justified band mapping exists, so none is fabricated.
- A challenge sample is present: challenge numbers are diagnostic only and deliberately score-stratified; they overstate detector performance relative to the population and must not be reported as headline metrics.

## Raw Metrics

Full machine-readable metrics are written to the companion `validation_results.json` file. The same payload is summarized here.

```json
{
  "dataset": {
    "reviews_loaded": 22,
    "distinct_places": 4,
    "places": [
      "p-coffee",
      "p-hotel",
      "p-rest",
      "p-shawarma"
    ],
    "fingerprint": "v1|22|4|a92e2e8945f168ce|9d5e8d8a7c4251d0|sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "embedding_model_name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "metric_definitions": {
      "templated": "Confusion matrix, precision = TP/(TP+FP), recall = TP/(TP+FN), F1, FPR = FP/(FP+TN), FNR = FN/(FN+TP) at the reporting threshold (default 65, matching the production templated signal cut). 'uncertain' human labels are excluded from binary metrics and counted separately.",
      "specificity": "Score distributions by human label; Spearman rank correlation between the human ordinal label (low=0/medium=1/high=2) and the ReviewScope specificity score; descriptive cross-tab with the production confidence bands (LOW<40, 40<=MEDIUM<65, HIGH>=65). No band mapping is invented.",
      "duplicate": "PRIMARY: pair-level precision/recall/F1 (GT pairs = pairs of reviews sharing a human duplicate_group_id; predicted pairs = pairs inside ReviewScope duplicate groups, restricted to labeled reviews). SECONDARY (descriptive): Jaccard>=0.5 group matching whose split/merge ambiguity is documented. The two are never blended."
    }
  },
  "label_coverage": {
    "total_label_records": 22,
    "matched_to_dataset": 22,
    "unmatched_review_ids": [],
    "by_label": {
      "templated_label": {
        "templated": 8,
        "organic": 11,
        "uncertain": 3
      },
      "template_group_id": {
        "set": 8
      },
      "specificity_label": {
        "low": 10,
        "high": 4,
        "medium": 7,
        "uncertain": 1
      },
      "duplicate_label": {
        "duplicate": 13,
        "unique": 8,
        "uncertain": 1
      },
      "sample_type": {
        "evaluation": 10,
        "challenge": 12
      },
      "sampling_stratum": {
        "random": 10,
        "high": 3,
        "duplicate": 4,
        "low": 5
      }
    },
    "n_template_groups_annotated": 8,
    "sample_type_counts": {
      "evaluation": 10,
      "challenge": 12
    }
  },
  "evaluation": {
    "label_count": 10,
    "representative": true,
    "templated": {
      "confusion": {
        "threshold": 65.0,
        "tp": 3,
        "fp": 0,
        "tn": 6,
        "fn": 1,
        "n_reference_positive": 4,
        "n_reference_negative": 6,
        "excluded_uncertain_and_missing": 0,
        "precision": 1.0,
        "recall": 0.75,
        "f1": 0.8571,
        "fpr": 0.0,
        "fnr": 0.25
      },
      "sweep": [
        {
          "threshold": 0.0,
          "precision": 0.4,
          "recall": 1.0,
          "f1": 0.5714,
          "support": 10
        },
        {
          "threshold": 5.0,
          "precision": 0.4,
          "recall": 1.0,
          "f1": 0.5714,
          "support": 10
        },
        {
          "threshold": 10.0,
          "precision": 0.4444,
          "recall": 1.0,
          "f1": 0.6154,
          "support": 10
        },
        {
          "threshold": 15.0,
          "precision": 0.5,
          "recall": 0.75,
          "f1": 0.6,
          "support": 10
        },
        {
          "threshold": 20.0,
          "precision": 0.6,
          "recall": 0.75,
          "f1": 0.6667,
          "support": 10
        },
        {
          "threshold": 25.0,
          "precision": 0.6,
          "recall": 0.75,
          "f1": 0.6667,
          "support": 10
        },
        {
          "threshold": 30.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 35.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 40.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 45.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 50.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 55.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 60.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 65.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 70.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 75.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 80.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 85.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 90.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 95.0,
          "precision": 1.0,
          "recall": 0.75,
          "f1": 0.8571,
          "support": 10
        },
        {
          "threshold": 100.0,
          "precision": null,
          "recall": 0.0,
          "f1": 0.0,
          "support": 10
        }
      ],
      "score_distribution_organic": {
        "n": 6,
        "min": 7.0,
        "p25": 14.2,
        "median": 15.6,
        "mean": 17.38,
        "p75": 25.1,
        "p95": 28.1,
        "max": 28.1
      },
      "score_distribution_templated": {
        "n": 4,
        "min": 14.2,
        "p25": 96.7,
        "median": 96.7,
        "mean": 76.08,
        "p75": 96.7,
        "p95": 96.7,
        "max": 96.7
      },
      "n_reference_organic": 6,
      "n_reference_templated": 4,
      "n_excluded_uncertain": 0
    },
    "specificity": {
      "distributions_by_human_label": {
        "high": {
          "n": 3,
          "min": 27.5,
          "p25": 27.5,
          "median": 82.0,
          "mean": 67.17,
          "p75": 92.0,
          "p95": 92.0,
          "max": 92.0
        },
        "low": {
          "n": 5,
          "min": 12.5,
          "p25": 12.5,
          "median": 12.5,
          "mean": 27.2,
          "p75": 12.5,
          "p95": 86.0,
          "max": 86.0
        },
        "medium": {
          "n": 2,
          "min": 60.0,
          "p25": 60.0,
          "median": 82.0,
          "mean": 71.0,
          "p75": 82.0,
          "p95": 82.0,
          "max": 82.0
        }
      },
      "spearman_human_ordinal_vs_score": 0.6095,
      "crosstab_human_vs_production_band": {
        "LOW": {
          "low": 4,
          "medium": 0,
          "high": 1
        },
        "MEDIUM": {
          "low": 0,
          "medium": 1,
          "high": 0
        },
        "HIGH": {
          "low": 1,
          "medium": 1,
          "high": 2
        }
      },
      "n_ordinal_pair": 10,
      "near_band_disagreements": 1,
      "far_band_disagreements": 2,
      "far_band_examples": [
        {
          "human_label": "high",
          "production_band": "LOW",
          "count": 1
        },
        {
          "human_label": "low",
          "production_band": "HIGH",
          "count": 1
        }
      ]
    },
    "duplicate": {
      "definitions": {
        "primary": "pair-level: precision = TP/(TP+FP), recall = TP/(TP+FN), where TP/FP/FN are unordered review pairs inside human duplicate groups (ground truth) vs pairs inside ReviewScope duplicate groups (predicted), restricted to labeled reviews.",
        "secondary": "group-level Jaccard>=0.5 match between a human group and a ReviewScope group. Descriptive only: split/merge ambiguity makes group matching inexact; it is never blended with pair-level numbers."
      },
      "pair_metrics": {
        "tp_pairs": 3,
        "fp_pairs": 0,
        "fn_pairs": 1,
        "known_duplicate_pairs_recovered": 3,
        "known_duplicate_pairs_total": 4,
        "missed_duplicate_pairs": 1,
        "false_positive_pairs": 0,
        "precision": 1.0,
        "recall": 0.75,
        "f1": 0.8571
      },
      "group_metrics_secondary": {
        "n_human_groups": 3,
        "n_predicted_groups": 3,
        "recovered_human_groups": 1,
        "missed_human_groups": 2,
        "missed_human_group_ids": [
          "group_0",
          "group_1"
        ],
        "matched_predicted_groups": 3,
        "unmatched_predicted_groups": 0,
        "pct_human_groups_recovered_jaccard_0_5": 0.3333,
        "pct_predicted_groups_matched_jaccard_0_5": 1.0
      },
      "excluded_uncertain_or_inconsistent": 0,
      "warnings": []
    },
    "templated_disagreement_count": 1,
    "sample_note": "Representative: deterministic simple random sample; headline population metrics are reported from this partition only."
  },
  "challenge": {
    "label_count": 12,
    "representative": false,
    "templated": {
      "confusion": {
        "threshold": 65.0,
        "tp": 3,
        "fp": 1,
        "tn": 4,
        "fn": 1,
        "n_reference_positive": 4,
        "n_reference_negative": 5,
        "excluded_uncertain_and_missing": 3,
        "precision": 0.75,
        "recall": 0.75,
        "f1": 0.75,
        "fpr": 0.2,
        "fnr": 0.25
      },
      "sweep": [
        {
          "threshold": 0.0,
          "precision": 0.4444,
          "recall": 1.0,
          "f1": 0.6154,
          "support": 9
        },
        {
          "threshold": 5.0,
          "precision": 0.4444,
          "recall": 1.0,
          "f1": 0.6154,
          "support": 9
        },
        {
          "threshold": 10.0,
          "precision": 0.4444,
          "recall": 1.0,
          "f1": 0.6154,
          "support": 9
        },
        {
          "threshold": 15.0,
          "precision": 0.4286,
          "recall": 0.75,
          "f1": 0.5455,
          "support": 9
        },
        {
          "threshold": 20.0,
          "precision": 0.4286,
          "recall": 0.75,
          "f1": 0.5455,
          "support": 9
        },
        {
          "threshold": 25.0,
          "precision": 0.4286,
          "recall": 0.75,
          "f1": 0.5455,
          "support": 9
        },
        {
          "threshold": 30.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 35.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 40.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 45.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 50.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 55.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 60.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 65.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 70.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 75.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 80.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 85.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 90.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 95.0,
          "precision": 0.75,
          "recall": 0.75,
          "f1": 0.75,
          "support": 9
        },
        {
          "threshold": 100.0,
          "precision": null,
          "recall": 0.0,
          "f1": 0.0,
          "support": 9
        }
      ],
      "score_distribution_organic": {
        "n": 5,
        "min": 11.0,
        "p25": 25.1,
        "median": 26.0,
        "mean": 37.38,
        "p75": 28.1,
        "p95": 96.7,
        "max": 96.7
      },
      "score_distribution_templated": {
        "n": 4,
        "min": 14.3,
        "p25": 96.7,
        "median": 96.7,
        "mean": 76.1,
        "p75": 96.7,
        "p95": 96.7,
        "max": 96.7
      },
      "n_reference_organic": 5,
      "n_reference_templated": 4,
      "n_excluded_uncertain": 3
    },
    "specificity": {
      "distributions_by_human_label": {
        "high": {
          "n": 1,
          "min": 100.0,
          "p25": 100.0,
          "median": 100.0,
          "mean": 100.0,
          "p75": 100.0,
          "p95": 100.0,
          "max": 100.0
        },
        "low": {
          "n": 5,
          "min": 12.5,
          "p25": 12.5,
          "median": 12.5,
          "mean": 12.5,
          "p75": 12.5,
          "p95": 12.5,
          "max": 12.5
        },
        "medium": {
          "n": 5,
          "min": 27.5,
          "p25": 27.5,
          "median": 33.0,
          "mean": 41.6,
          "p75": 60.0,
          "p95": 60.0,
          "max": 60.0
        },
        "uncertain": {
          "n": 1,
          "min": 60.0,
          "p25": 60.0,
          "median": 60.0,
          "mean": 60.0,
          "p75": 60.0,
          "p95": 60.0,
          "max": 60.0
        }
      },
      "spearman_human_ordinal_vs_score": 0.9535,
      "crosstab_human_vs_production_band": {
        "LOW": {
          "low": 5,
          "medium": 3,
          "high": 0
        },
        "MEDIUM": {
          "low": 0,
          "medium": 2,
          "high": 0
        },
        "HIGH": {
          "low": 0,
          "medium": 0,
          "high": 1
        }
      },
      "n_ordinal_pair": 11,
      "near_band_disagreements": 3,
      "far_band_disagreements": 0,
      "far_band_examples": []
    },
    "duplicate": {
      "definitions": {
        "primary": "pair-level: precision = TP/(TP+FP), recall = TP/(TP+FN), where TP/FP/FN are unordered review pairs inside human duplicate groups (ground truth) vs pairs inside ReviewScope duplicate groups (predicted), restricted to labeled reviews.",
        "secondary": "group-level Jaccard>=0.5 match between a human group and a ReviewScope group. Descriptive only: split/merge ambiguity makes group matching inexact; it is never blended with pair-level numbers."
      },
      "pair_metrics": {
        "tp_pairs": 3,
        "fp_pairs": 4,
        "fn_pairs": 1,
        "known_duplicate_pairs_recovered": 3,
        "known_duplicate_pairs_total": 4,
        "missed_duplicate_pairs": 1,
        "false_positive_pairs": 4,
        "precision": 0.4286,
        "recall": 0.75,
        "f1": 0.5455
      },
      "group_metrics_secondary": {
        "n_human_groups": 4,
        "n_predicted_groups": 3,
        "recovered_human_groups": 1,
        "missed_human_groups": 3,
        "missed_human_group_ids": [
          "group_0",
          "group_1",
          "group_2"
        ],
        "matched_predicted_groups": 3,
        "unmatched_predicted_groups": 0,
        "pct_human_groups_recovered_jaccard_0_5": 0.25,
        "pct_predicted_groups_matched_jaccard_0_5": 1.0
      },
      "excluded_uncertain_or_inconsistent": 1,
      "warnings": []
    },
    "templated_disagreement_count": 2,
    "sample_note": "Diagnostic only: score/duplicate-enriched challenge sample. These numbers are NOT representative of population performance and MUST NOT be quoted as headline precision/recall/F1/FPR/FNR."
  }
}
```
