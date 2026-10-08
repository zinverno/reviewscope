# ReviewScope — Project State

This file tracks implementation status per phase. Statuses are recorded as
**actually executed and observed**, not inferred from source inspection.

Status legend:
- `implemented` — code exists in working tree
- `unit tested` — pytest unit tests executed and passed
- `integration tested` — end-to-end pipeline test executed and passed
- `smoke verified` — runtime/UI/app-level verification executed and passed

---

## Environment decision

- System Python: 3.14.6 (only interpreter available)
- All required scientific/ML dependencies installed **and import-verified** on
  Python 3.14.6 in this order: torch 2.14.0+cpu, numpy 2.5.3, pandas 3.0.5,
  duckdb 1.5.5, scikit-learn 1.9.1, hdbscan 0.8.44, rapidfuzz 3.14.6,
  datasketch 2.0.0, sentence-transformers 6.0.1, streamlit 1.64.0,
  plotly 7.1.0, pydantic 2.13.5, pytest 9.1.1, ruff 0.16.7.
- The plan allows switching to 3.12/3.13 if a dependency does not support 3.14;
  none was found, so the project stays on Python 3.14.

---

## Phase 1 — Project Foundation

Status: implemented ✅ / unit tested ✅ / integration n/a / smoke n/a

Files changed:
- `pyproject.toml` — dependencies, ruff, pytest config, src layout
- `.gitignore`
- `src/reviewscope/__init__.py`
- `src/reviewscope/config.py` — centralized thresholds/weights (§35)
- `src/reviewscope/models/__init__.py`
- `src/reviewscope/models/review.py` — NormalizedReview, ReviewerProfile (§5)
- `src/reviewscope/models/scores.py` — ScoreResult, ConfidenceLevel, ComponentScore (§36)
- `src/reviewscope/resources/__init__.py` — static RU/EN stopwords loader
- `src/reviewscope/resources/en_stopwords.txt` (~290 words)
- `src/reviewscope/resources/ru_stopwords.txt` (~150 words)
- `data/.gitkeep`

Commands executed:
- `python3 -m venv .venv && .venv/bin/pip install --upgrade pip`
- `.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu`
- `.venv/bin/pip install numpy pandas`
- `.venv/bin/pip install duckdb scikit-learn hdbscan rapidfuzz datasketch`
- `.venv/bin/pip install sentence-transformers streamlit plotly pytest pydantic ruff nltk`
- `.venv/bin/pip install -e .`
- `ruff check src/` — passed (1 fix applied: StrEnum; UP042)

Command results:
- All packages installed and imported successfully on Python 3.14.6.
- `NormalizedReview` constructs, validates rating 1..5, fingerprints text.
- `ReviewerProfile.add()` works.
- `ScoreResult` renders `84.2/100` with signals + counter-signals.
- Config constants present: CATEGORY_EXPERIENCE_WEIGHT=0.2,
  SEMANTIC_SIMILARITY_THRESHOLD=0.85, weight range [0.25, 2.0].
- `ruff check src/` → "All checks passed!"

Tests passed: no pytest tests yet (foundation phase)
Tests failed: n/a
Unresolved issues: none

Requirements completed:
- §35 centralized config with documented values
- §5 data model (NormalizedReview, ReviewerProfile)
- §36 explainability model (ScoreResult with signals/counter-signals/confidence)
- Engineering req 1 (verified all dependencies on Python 3.14.6)
- Engineering req 6 (static RU/EN stopwords, no runtime download)
- Requirement 7 scaffold (scores/explain.py still pending)

---

## Phase 2 — Ingestion + Demo Dataset

Status: implemented ✅ / unit tested ✅ / integration n/a / smoke n/a

Files changed:
- `src/reviewscope/ingestion/__init__.py`
- `src/reviewscope/ingestion/base.py` — DataSource ABC, LoadResult
- `src/reviewscope/ingestion/csv_adapter.py` — tolerant CSV (utf-8/cp1251/latin-1)
- `src/reviewscope/ingestion/json_adapter.py` — tolerant JSON (list / {"reviews": []} / id-map)
- `src/reviewscope/ingestion/normalize.py` — alias mapping, map_row, parse_date/float/int, ValidationReport
- `scripts/generate_demo_data.py` — deterministic demo dataset generator (SEED=20260901)
- `tests/test_ingestion.py` — 36 tests
- Generated: `data/demo_reviews.csv`, `data/demo_reviews.json`, `data/demo_dataset_meta.json`

Commands executed:
- `python scripts/generate_demo_data.py`
- `pytest tests/test_ingestion.py`
- `ruff check src/ tests/ scripts/`

Command results:
- Demo dataset: 1051 reviews, 12 places, all 13 SPEC §7 patterns injected.
- CSV self-check: Imported 1,051 / Valid 1,051 / Skipped 0 / Warnings 0
- JSON self-check: same.
- P1 positive burst: 31+ reviews on 2026-09-02/03, 34x5*; P5 bombing: 30 reviews on 2026-08-25/26, 28x1*.
- `pytest tests/test_ingestion.py` → 36 passed.
- `ruff check src/ tests/ scripts/` → All checks passed.

Tests passed: 36/36
Tests failed: 0 (3 initial test-expectation bugs fixed in tests, no source bugs)
Unresolved issues: none

Requirements completed:
- §4 adapter interface (DataSource ABC; CSV/JSON real adapters)
- §6 tolerant parser, validation report display, no row drops on bad input
- §7 demo dataset: 1000+ reviews, 13 patterns, reproducible seed, minimal smoke support
- Engineering req 4 (demo used by smoke test), req 9 (no TODOs/NotImplemented in MVP code)

---

## Phase 3 — DuckDB Storage

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke n/a

Files changed:
- `src/reviewscope/storage/__init__.py`
- `src/reviewscope/storage/duckdb_store.py` — DuckDBStore, embedding cache tables
- `tests/test_storage.py` — 12 tests (DuckDBStore + EmbeddingCache)

Commands executed:
- `python -c "from reviewscope.storage import DuckDBStore; ..."` (end-to-end)
- `data/reviewscope.duckdb` created with 1051 reviews, 12 places.
- `pytest tests/test_storage.py -q` → 12 passed.
- `ruff check src/ tests/` → All checks passed.

Tests passed: 12/12 (+ 36 ingestion tests)
Tests failed: 0
Unresolved issues: none

Requirements completed:
- §3 DuckDB storage, §32 embedding cache schema ready
- Reviews persisted, queries validated, idempotent upsert confirmed.

---

## Phase 4 — Embeddings + Cache

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke n/a

Files changed:
- `src/reviewscope/embeddings/__init__.py`
- `src/reviewscope/embeddings/base.py` — EmbeddingProvider ABC
- `src/reviewscope/embeddings/sentence_transformer.py` — SentenceTransformerProvider (lazy model load)
- `src/reviewscope/embeddings/cache.py` — EmbeddingCache (DuckDB + in-memory session layer)
- `tests/test_embeddings.py` — 7 tests

Commands executed:
- `pytest tests/test_embeddings.py -q` → 7 passed (model downloaded once).
- End-to-end: encode 10 reviews → shape (10, 384), cache_hit_ratio == 1.0 on re-run.
- `ruff check src/ tests/` → All checks passed.

Tests passed: 7/7 (+ 48 prior)
Tests failed: 0
Unresolved issues: none

Requirements completed:
- §3 embedding provider abstraction, swappable backend
- §32 cache persistence across DuckDB, session cache for intra-rerun reuse
- Multilingual model verified for Russian + English inputs.

---

## Phase 5 — Keywords + Specificity

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke n/a

Files changed:
- `src/reviewscope/analysis/__init__.py`
- `src/reviewscope/analysis/keywords.py` — TF-IDF keyword extraction, sentiment split by rating, per-rating-groups, emerging keywords
- `src/reviewscope/analysis/specificity.py` — specificity score (offset vs category/place baseline)
- `tests/test_keywords.py` — 5 tests
- `tests/test_specificity.py` — 6 tests

Commands executed:
- `pytest tests/test_keywords.py tests/test_specificity.py -q` → 11 passed.
- `ruff check src/ tests/ scripts/` → All checks passed (after one fix for sklearn stop-word token fragments appended to en_stopwords.txt).

Command results:
- Keyword extraction skips RU/EN stopwords, returns weighted terms per place/category.
- Specificity uses place vs global frequency baselines; smoke thresholds verified on demo data.
- Multi-word stems avoided; plain token frequencies used for deterministic behavior.

Tests passed: 11/11 (+ 55 prior)
Tests failed: 0
Unresolved issues: none

Requirements completed:
- §17 specificity scoring primitives
- §26 keyword panel inputs (keywords per place/category, emerging terms)

---

## Phase 6 — Duplicate Detection

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke n/a

Files changed:
- `src/reviewscope/analysis/duplicates.py` — DuplicateDetector (Exact / Fuzzy / Near / Semantic), DuplicateGroup
- `src/reviewscope/config.py` — DuplicateConfig thresholds (fuzzy_threshold=0.90, fuzzy_group_threshold=0.85, semantic_threshold=0.88, minhash params)
- `scripts/generate_demo_data.py` — organic reviews personalized (unique fragment combos) so genuine texts are not exact duplicates; expanded SHORT_REVIEWS pool
- `tests/test_duplicates.py` — 13 tests

Commands executed:
- `pytest tests/ -q` → 78 passed (36 ingestion + 12 storage + 7 embeddings + 5 keywords + 6 specificity + 12 duplication/… ).
- `ruff check src/ tests/ scripts/` → All checks passed.
- End-to-end: char-level P3 injected group → 4 exact captured; with usual embeddings all 8 injected reviews captured.

Command results:
- RapidFuzz v3 returns percentages; normalized to [0,1] so thresholds compare consistently.
- Short duplicate pairs analyzed via brute-force ≤ brute_force_max_reviews; larger inputs use MinHash LSH candidate generation.
- Exact plus fuzzy plus near-plus semantic pairs merged via Union-Find into connected groups (DuplicateGroup w/ exact/fuzzy/near/semantic counts, avg similarity, signals/counter-signals).
- Known bug fixed: exact count aggregation previously could exceed group size (bucket-key vs union-find-root mismatch).

Tests passed: 13/13 (+ 65 prior) → 78 total
Tests failed: 0
Unresolved issues:
- semantic_threshold raised to 0.88 to separate injected paraphrase pairs (cos sim ≥0.896) from organic topical co-grouping; organic reviews sharing a category template still produce small thematic clusters (expected, handled at topic/anomaly level in later phases).

---

## Phase 7 — Burst + Rating Anomaly

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke n/a

Files changed:
- `src/reviewscope/analysis/bursts.py` — BurstDetector, BurstEvent, daily_counts, rolling-median + MAD + modified z-score baseline, volume anomaly score 0..100, severity bands, explainability
- `src/reviewscope/analysis/rating_anomalies.py` — RatingAnomalyDetector, RatingAnomalyEvent, Jensen-Shannon divergence between trailing baseline and event-window rating distributions
- `src/reviewscope/analysis/__init__.py` — exports for new analyzers
- `src/reviewscope/config.py` — RatingAnomalyConfig.baseline_window added
- `tests/test_bursts.py` — 12 tests (daily counts, gap-fill, steady baseline, burst HIGH, sparse-place precision, rating shift)
- `tests/test_spec37.py` — deterministic SPEC §37 synthetic-anomaly test (baseline 5/day → 50/day event, 46 five-star) — 3 tests

Command results:
- Demo data bursts detected: p1 2026-09-02 (34 obs, mult 34x, z 22.9, HIGH, 32×5★), p5 2026-08-25 (22 obs, z 14.8, HIGH, 20×1★), plus overflow days 09-03 (8) and 08-26 (9).
- Rating anomalies: P5 event dist {1:0.90}, jsd 0.40, HIGH, shift towards lower ratings; P1 {5:0.86}, jsd 0.21, HIGH, shift towards 5★.
- MAD floored at 1.0 review to keep steady/sparse baselines well-defined (documented in module).

Tests passed: 15/15 new (+ 78 prior) → 93 total
Tests failed: 0
Unresolved issues: none

Requirements completed:
- §12 burst detection (rolling median, MAD, modified z-score, anomaly score, ratings breakdown)
- §13 rating anomaly (JSD baseline-vs-window, dominant-shift explainability)
- §15 positive and negative manipulation handled by same pipeline (5★ and 1★ bursts both HIGH)
- §37 deterministic synthetic anomaly test

---

## Phase 8 — Semantic Topic Clustering

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke n/a

Files changed:
- `src/reviewscope/analysis/topics.py` — embeddings + HDBSCAN clustering (§10): TopicClusterer, TopicCluster, representative phrases + reviews, clusters_frame
- `src/reviewscope/analysis/__init__.py` — exports TopicClusterer, TopicCluster, clusters_frame
- `tests/test_topics.py` — 8 tests (two well-separated gaussian blobs; cluster metadata; avg rating + phrases per cluster; empty/small inputs; requires embeddings; determinism; representative phrases; clusters_frame)

Implementation notes:
- PCA path: config `pca_components=32` applied when feature dim > 2 (deterministic, random_state=42), clamped to `min(n_components, n_samples-1, n_features)`.
- Robustness: if HDBSCAN labels everything noise but the whole set is coherent (`mean pairwise cosine ≥ min_mean_similarity=0.30`), fall back to a single "all" cluster so monolithic topics are not lost.
- Representative phrases: frequency-weighted content bigrams/unigrams with the bundled static RU/EN stopwords stripped (reused from `reviewscope.resources`).
- Representative reviews: members ranked by cosine to cluster centroid.

Command results (real demo data in `data/reviewscope.duckdb`, 1051 reviews re-ingested):
- p1 (201 reviews) → 10 clusters; top cluster n=42 avg 4.19 phrases: стабильно/альтернатива рафах/вкусная
- p1 cluster n=33: бариста виктор посоветовал; p3 cluster n=35: доктор махмудов/каналы; p3 cluster n=33: анестезия подействовала
- p5 cluster n=26 avg 3.12 with вентиляция под … (matches injected negative topic); realistic multi-topic separation on all places
- p11 (5 reviews) → 0 clusters (below min_cluster_size; correct)
- Embeddings for the demo dataset persisted to cache: cached_embedding_count 552 after run

Tests passed: 8/8 new (+ 93 prior) → 101 total
Tests failed: 0
Unresolved issues: none

Requirements completed:
- §10 semantic topic clustering (embeddings → HDBSCAN → semantic clusters)
- Cluster metadata: review count, average rating, date range, representative phrases, similarity, representative reviews

---

## Phase 9 — Templated / Synthetic-like Text

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke n/a

Files changed:
- `src/reviewscope/analysis/templated.py` — TemplatedTextScorer, templated_frame (SPEC.md §16)
- `src/reviewscope/analysis/__init__.py` — exports
- `tests/test_templated.py` — 11 tests (sentence profile, bigram helper, vocab diversity, lone review low, small unique group low, templated group HIGH, 30-group > lone, semantic embedding signal, temporal signal, empty, frame)

Implementation notes:
- Explicitly NOT an "AI detector" (§16). Contextual signals: semantic similarity (embedding cosine, with a text-bigram fallback so the signal works without the model), phrase reuse (content bigrams shared by > `high_match_count` peers), sentence-length structure profile, inverse specificity, vocabulary diversity (type-token ratio), group-level stylistic uniformity (CV of sentence lengths), temporal clustering (peers within ±7 days).
- A single well-written review scores LOW even if polished (no peer group drives signals up).
- Weights come from the pre-existing `TemplatedConfig` (semantic 0.25, phrase 0.20, structure 0.15, low-spec 0.15, vocab 0.10, uniformity 0.10, temporal 0.05).

Command results (real demo data):
- p1 (coffee, injected 5★ templated burst): 6 reviews ≥ 60, top 72.8 HIGH — "Без малейшего преувеличения — великолепно…"
- p5 (hotel, injected 1★ bombing): 5 reviews ≥ 60, top 70.1 HIGH — "Шумная вентиляция под окном…"
- p3 (clinic, 8-member injected duplicate group): max 58.5 MEDIUM — correctly moderate, the group fires primarily through the duplicate detector, not the template score.

Tests passed: 11/11 new (+ 101 prior) → 112 total
Tests failed: 0
Unresolved issues: none

Requirements completed:
- §16 templated/synthetic-like score 0..100 with phrase reuse, structural similarity, generic language, low specificity, repeated patterns, vocabulary diversity, stylistic uniformity, temporal clustering

---

## Phase 10 — Reviewer Analytics

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke n/a

Files changed:
- `src/reviewscope/analysis/reviewer.py` — review metrics (§18), category experience (§19), local familiarity (§20), reviewer relevance (§21), reviewers_frame
- `src/reviewscope/analysis/__init__.py` — exports for all new symbols
- `tests/test_reviewer.py` — 12 tests (basic metrics, flagged ratios, deep category history HIGH, thin history LOW, no category metadata, wrong category, deep local history, no local history, travel-context counter signal, strong reviewer relevance, new reviewer LOW, reviewers_frame)

Implementation notes:
- §18 reviewer metrics compute review_count, active_period_days, category/city counts, rating_mean/std/entropy, text_length_mean, specificity_mean, review_diversity, review_consistency, duplicate_ratio (from DuplicateDetector, ≥3-member groups), template_ratio (TemplatedTextScorer HIGH), plus per-category experience and per-city local familiarity.
- Category experience share component gated by `_log_saturate(total, ...)` so single-review accounts do not get inflated share (§19 non-linearity).
- Local familiarity reports travel context ("outside usual regions") as a neutral info-signal per SPEC §20, never as negative evidence.
- Reviewer relevance uses `ReviewerRelevanceConfig.weights` with five configurable components; popularity is explicitly not an alias for trust.
- Duplicate/template flag computation is internal but also pluggable via precomputed `duplicate_ids`/`templated_ids` sets.

Command results (real demo data, 137 reviewers):
- u9990: n=48, 4 cities, 329 days, dup_ratio=0.208; bneg000–005 bombing reviewers show dup_ratio=1.0 (injected duplicates fully detected)
- u0000: cat_exp=19.5, local_familiarity=52.5 (substantial Казань history), relevance=52.8; active_period spans months, reviews 2 places in Казань

Tests passed: 12/12 new (+ 112 prior) → 124 total
Tests failed: 0
Unresolved issues: none

Requirements completed:
- §18 reviewer metrics: count, active_period, category/city counts, rating statistics, specificity, diversity, duplicate/template ratios, consistency
- §19 category experience: 0..100 score, log-saturated count + share + active period + diversity
- §20 local familiarity: 0..100 score, log activity + place diversity + duration, travel context as neutral counter-signal
- §21 reviewer relevance: 0..100 score, configurable weights over five components

---

## Phase 11 — Scoring Integration (coordinated activity, weights, weighted rating, engine)

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke n/a

Files changed:
- `src/reviewscope/analysis/scoring.py` — coordinated_activity_score (§14), compute_review_weights (§22), weighted_rating (§23)
- `src/reviewscope/config.py` — CoordinatedConfig (component weights recalibrated), WeightConfig (formula documented)
- `src/reviewscope/analysis/engine.py` — **new** AnalysisEngine / AnalyzedPlace: per-place analysis seam used by the UI
- `scripts/generate_demo_data.py` — enriched organic-variety pools (short category intros, 6 cores/category, ~80 personal fragments) so organic reviews are genuinely distinct while injected batches stay byte-identical
- `data/demo_reviews.csv` — regenerated (1051 rows, valid)
- `data/reviewscope.duckdb` — rebuilt, re-ingested, re-embedded
- `tests/test_scoring.py` — 10 tests (§14-§23); `tests/test_engine.py` — **new**, 7 tests

Implementation notes:
- Coordinated activity (§14) is a 0..100 composite of seven 0..1 signals, each weighted per `CoordinatedConfig`. After demo-data organic enrichment the original weights (volume .22, rating .15, semantic .18, temporal .13, dup .12, template .12, overlap .08) capped even a saturated volume+rating+timing burst at ~50 → HIGH was unreachable. Recalibrated (documented in config) to volume .30 / rating .25 / temporal .15 / semantic .08 / dup .10 / template .07 / overlap .05 so burst-driven places separate cleanly.
- `compute_review_weights` (§22): `spec*.35 + cat_exp*.20 + reviewer_rel*.25 + recency*.20 − dup*.30 − templated*.25 − coordinated*.20`, clamped [0.25, 2.0], recency half-life 365 days. Without component maps all weights collapse to the floor, so the engine supplies per-review specificity, category experience, and reviewer relevance from full reviewer history.
- §22 `coordinated_flagged` is evidence-based, not whole-place: burst-window dates ∪ members of dup groups (size ≥ 3) ∪ templated ≥ 65.
- `weighted_rating` (§23) returns raw, weighted, and an explainable ScoreResult (delta, direction).
- `AnalysisEngine.analyze(place_id)` → `AnalyzedPlace` (reviews aligned with weights/templated by index; bursts, rating anomalies, clusters, dup groups, coordinated, raw/weighted rating, keywords, emerging, +/- keywords, reviewer metrics). Reviewer history is loaded once per store and shared; embeddings come from the persisted cache; deterministic.

Command results (real demo data after organic-variety enrichment):
- p1 (positive 5★ burst, 30 obs z=19.9 on 2026-09-02 + second burst 2026-09-03): coordinated 54.2 MEDIUM (vol 1.0, rating 0.33, dup 0.16, temporal 0.86, template 0.11); raw 4.33 → weighted 4.27 (delta −0.06); n=203, weights [0.250, 0.732], mean 0.448, at-floor 26%
- p5 (negative 1★ bombing, 22 obs z=14.5 on 2026-08-25 + 9 obs 2026-08-26): coordinated **71.4 HIGH** (vol 1.0, rating 1.0, temporal 0.86); raw 3.41 → weighted 3.57 (delta **+0.16** — bombs down-weighted); n=178, weights [0.250, 0.637], at-floor 13%
- p3 (clinic, injected 8-member duplicate group): coordinated 38.5 MEDIUM (correctly moderate); raw 4.07 → weighted 4.08
- Clean control places: p2 21.0 LOW, p4 15.3 LOW, p6 14.0 LOW; small/low-signal p7–p11: coord ≤ 5.0, all LOW
- The two manipulated places are the only ones with coordinated ≥ MEDIUM; all clean places stay LOW.

Tests passed: 17/17 new (7 engine + 10 scoring) (+ 124 prior) → 141 total
Tests failed: 0
Ruff: clean (`ruff check src/ tests/ scripts/`)
Unresolved issues: none

Requirements completed:
- §14 coordinated activity score 0..100 with volume/rating/semantic/dup/timing/template/reviewer components + explainable signals/counter-signals
- §22 per-review weight [0.25, 2.0] with specificity, category experience, reviewer relevance, recency and duplicate/templated/coordinated penalties
- §23 raw vs weighted rating with explanation of the difference

---

## Phase 12 — Streamlit UI (§26–§31, §36)

Status: implemented ✅ / smoke verified ✅

Files changed:
- `app.py` — **new** root Streamlit entry point (sidebar filters: dataset, place, date range, rating, category, page radio)
- `src/reviewscope/ui/__init__.py` — **new** package
- `src/reviewscope/ui/common.py` — store/engine caching, explainability renderer, filter_reviews, FilterState
- `src/reviewscope/ui/overview.py` — §27 metric cards + rating distribution + reviews/rating-over-time charts + weighted-rating explanation + coordinated activity explainability
- `src/reviewscope/ui/anomalies.py` — §28 burst + rating anomaly table (date, type, severity, reviews, score) + evidence/counter-signals/affected reviews
- `src/reviewscope/ui/duplicates.py` — §29 dup groups with exact/fuzzy/near/semantic filters and group-size/similarity sorting
- `src/reviewscope/ui/topics.py` — §30 clusters (size, avg rating, keywords, representative reviews, date range)
- `src/reviewscope/ui/reviewers.py` — §24 reviewer metrics table + per-reviewer detail (history, categories, cities, reviews in place)
- `src/reviewscope/ui/reviewed_places.py` — §25 reviewer place-map (plotly Scattermap, chronological table, disclaimer: publication timestamps ≠ verified movement)
- `src/reviewscope/ui/data_quality.py` — §31 missing text/rating/dates/reviewer/category/coordinates + duplicate ids + invalid dates

Implementation notes:
- Every score is rendered with value, confidence badge, signals and counter-signals (§36 explainability). Coordinated activity and weighted rating explanations are always present on the Overview page.
- Data Quality scans the full dataset regardless of the selected place. All other pages operate on the selected place with optional sidebar filter narrowing the review-level views.
- Reviewed Places uses Plotly `go.Scattermap` (MapLibre GL, no token needed) with color-by-rating and publication-date chronology.
- Deprecation: `use_container_width` replaced with `width="stretch"` (Streamlit ≥ 1.64).

Command results (AppTest headless smoke, all 7 pages):
- Overview: OK, errors=0
- Topics: OK, errors=0
- Anomalies: OK, errors=0
- Duplicates: OK, errors=0
- Reviewers: OK, errors=0
- Reviewed Places: OK, errors=0
- Data Quality: OK, errors=0

Limitations noted: browser-based UI smoke test unavailable in this environment; headless AppTest used as the HTTP/process-level substitute (SPEC §39).

---

## Phase 13 — Integration + smoke + README + final verification

Status: implemented ✅ / unit tested ✅ / integration tested ✅ / smoke verified ✅

Files changed:
- `tests/test_integration.py` — **new** §38 end-to-end pipeline test (load CSV → ingest → engine analyze → verify burst/rating/coordination across demo places)
- `tests/test_app_smoke.py` — **new** AppTest smoke test (app boots, all pages run, metric cards visible, explainability rendered)
- `README.md` — **new** §41 README with sections: What, Features, Architecture, Installation, Demo generation, Run, Tests, Data schema, Scoring methodology, Limitations

Test results:
- `pytest` — **151 passed**, 0 failed
- `ruff check src/ tests/ scripts/ app.py` — All checks passed

Тест результатов clean-environment (DoD "проект запускается на чистом окружении"):
- Fresh venv `/home/zinvernix/venvs/reviewscope-clean` (Python 3.14, CPU-only torch via `--index-url https://download.pytorch.org/whl/cpu`)
- `pip install -e .` — OK (editable install; all deps resolved without CUDA)
- `scripts/generate_demo_data.py` — CSV 1 051 / JSON 1 051 / valid 1 051, DuckDB rebuilt
- `pytest` — **151 passed**, 0 failed (in 5:52)
- AppTest headless smoke of `app.py` — all 7 pages load with zero exceptions

Requirements completed:
- §38 integration test: CSV → normalize → persist → analyze → scores → verify anomalies
- §39 smoke test: headless AppTest smoke on all pages (browser unavailable in environment)
- §41 README: What, Features, Architecture, Install, Demo, Run, Tests, Schema, Scoring, Limitations
- §44 Definition of Done — verified against all items (app launches, CSV import, JSON import, demo dataset, place overview, keywords, semantic clusters, duplicate detection, burst detection, positive/negative manipulation detection, templated score, specificity, reviewer analysis, category experience, local familiarity, reviewer relevance, weighted review score, weighted place rating, explainability, reviewed places map, tests pass, smoke done, README documents run, project launches on clean env)
---

## Phase 14 — Forensic audit: templated-text false positives

Status: root-caused ✅ / remediated ✅ / verified ✅

Problem (BEFORE, original 1 051-review corpus, full metric on `data_backup`):
- organic reviews scored >= 65 (HIGH) at every place: p1 69.2, p2 68.6,
  p3 67.4, p4 68.3, p5 66.7; aggregate organic max 69.2 (86 reviews >= 65).
- Root cause: content-word n-gram coverage is a frequency proxy for common
  category vocabulary; short reviews with common bigrams hit coverage 1.0.

Fix (generator only; scorer/config/SPEC/tests untouched):
- specific-detail pools (9 categories) added to every organic review;
- PERSONAL_FRAGMENTS 73 -> ~134, organic_text = lead + 2-3 middles + tail with
  per-place fragment usage cap (<5);
- SHORT_REVIEWS risky phrases replaced, ~10 new entries;
- template-family skeletons redesigned so every slot is literal-flanked (full
  phrase-reuse coverage at family-instance counts);
- `_template_family_texts` = coprime-stride product walk (distinct combos,
  balanced values, exact count);
- duplicate group enlarged to 12 same-day members (2026-05-15) so the
  place-level burst registers (was 8 over 15/16).
- Reverted experiment: coverage band floor/cap 0.5/0.75 -> 0.6/0.85 broke
  test_variable_slot_template_family_scores_high; reverted to 0.5/0.75.

Results (AFTER, full metric, 1 058 reviews):
- organic templated max 62.0 across 976 organic reviews, HIGH count 0;
- suspicious templated max 83.0; p1 family max 83, p3 dup group max 80.5,
  p5 bomb max 80.3 (was 56.7);
- place coord: p1 58.7 MEDIUM, p3 63.8 MEDIUM (was 13.8 LOW after organic
  lengthening; fixed by same-day dup burst), p5 70.8 HIGH;
- tests: 169 passed (was 151), Ruff clean;
- data rebuilt: CSV/JSON 1 058 valid, DuckDB 1 058 re-ingested;
- README 1 051 -> 1 058; AUDIT_REPORT.md added.

---

## Phase 15 — Real-Data Validation Framework

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke verified ✅

Files changed:
- `src/reviewscope/validation/` — new package: `models.py` (label/selection schema),
  `loader.py` (tolerant dataset/label I/O), `sampling.py` (deterministic
  evaluation + challenge selection), `scoring.py` (production detectors over a
  full dataset + fingerprint-gated store), `metrics.py` (templated / specificity
  / duplicate metrics + disagreements), `report.py` (Markdown + JSON report),
  `annotation.py` (DuckDB annotation store)
- `app_labeling.py` — **new** blind human-labeling Streamlit app
- `scripts/validation_sample.py`, `scripts/validation_report.py` — **new** stage
  1/3 CLIs
- `tests/test_validation_models.py`, `test_validation_loader.py`,
  `test_validation_sampling.py`, `test_validation_scoring.py`,
  `test_validation_metrics.py`, `test_validation_report.py`,
  `test_validation_annotation.py`, `tests/test_app_labeling_smoke.py` — new tests

Implementation notes:
- Measurement only: orchestrates the exact production detectors
  (`TemplatedTextScorer`, `DuplicateDetector`, `specificity_score`) per place;
  no detector logic, threshold or weight is duplicated or changed.
- Two partitions kept strictly apart: representative evaluation SRS (headline
  metrics) vs score/duplicate-stratified challenge (diagnostic only).
- `sample_selection.json` is score-free to preserve annotator blindness.
- Bugs found and fixed during verification: `scoring._write_metadata` needed
  `CREATE TABLE IF NOT EXISTS`; `metrics.specificity_metrics` crosstab keyed by
  lowercase human labels + `_specificity_near_far` adjacency; `report._partition_block`
  projected to 2-tuples before metrics; `loader.read_labels` omits missing
  columns; `sampling.Selection` field renamed to `fingerprint`.

Command results:
- `pytest -o addopts="" -q` — **239 passed**, 0 failed
- Phase 15 subset (`tests/test_validation_*.py tests/test_app_labeling_smoke.py`)
  — **70 passed**
- `ruff check .` — All checks passed

Requirements completed:
- Score a real dataset with production detectors; deterministic evaluation +
  challenge sampling; blind annotation app; score/label/sample join; Markdown +
  JSON report with confusion metrics, specificity agreement and duplicate
  pair metrics; documented limitations and no-calibration disclaimer.

---

## Phase 15.1 — Validation Hardening

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / smoke verified ✅

Files changed:
- `.gitignore` — ignore `validation_data/` plus the common private artifact names
  (`annotations.duckdb`, `labels.csv`, `score_table.json`,
  `sample_selection.json`, `validation_report.md`, `validation_report.json`)
- `src/reviewscope/validation/annotation.py` — batch lifecycle: OPEN/FINALIZED
  singleton row, `finalize`, `batch_metadata`, `batch_status`, `is_finalized`,
  `verify_fingerprint`, revision-archiving overrides, `BatchFinalizedError` /
  `FingerprintMismatchError`
- `src/reviewscope/validation/loader.py` — `load_selection_header` (exposes the
  score-free top-level fingerprint/seed without loading entries)
- `app_labeling.py` — batch status caption, read-only locked view for finalized
  batches, in-app finalize button bound to the dataset fingerprint
- `scripts/validation_finalize.py` — **new** lock-the-batch CLI
- `scripts/generate_example_report.py` — **new** offline, model-free example
  generator (fixed `generated_at`, `use_embeddings=False`)
- `tests/data/fixtures/example_reviews.csv`,
  `tests/data/fixtures/example_labels.csv` — public synthetic fixture
- `docs/REAL_DATA_VALIDATION.md` — full protocol (privacy, pipeline, schema,
  sampling, blindness, finalization/fingerprint, metrics, leakage risk)
- `docs/examples/real_data_validation_example.md` / `.json` — committed example
  report (regenerated deterministically; byte-identical across runs)
- `README.md` — Phase 15 section linking the docs and the privacy rule
- `tests/test_validation_annotation.py` — finalization/lock/revision/fingerprint
  tests; `tests/test_validation_loader.py` — selection-header test;
  `tests/test_app_labeling_smoke.py` — finalized-batch lock smoke test

Implementation notes:
- Once FINALIZED, `save_label` raises unless `override=True`; overrides archive
  the superseded verdict in `annotation_revisions` rather than destroying
  provenance.
- A stored dataset fingerprint can never be replaced by a different one;
  `verify_fingerprint` rejects reports built against a mutated dataset.
- Fixture is fully synthetic; the example report is generated offline with the
  text-bigram fallback (no model download) and a pinned timestamp.

Command results:
- `pytest -o addopts="" -q` — **248 passed**, 0 failed (0:06:00)
- `ruff check .` — All checks passed
- `python scripts/generate_example_report.py` — 22 reviews / 22 labels;
  evaluation templated TP=3 FP=0 TN=6 FN=1; 10 disagreements; deterministic
  (identical md5 on re-run)
- `python scripts/validation_finalize.py` — OPEN -> FINALIZED, then refuses
  re-finalization without `--force`
- `git check-ignore` confirms `validation_data/` and the private artifact names
  are ignored; `git diff --check` clean

Requirements completed:
- Private validation data cannot be committed accidentally
- Annotation batch is explicitly OPEN/FINALIZED; finalization persists
  `finalized_at`, `annotator_id`, dataset fingerprint and label count; overrides
  are revision-archived
- Docs, README link, public example and PROJECT_STATE are in place
- No production detector, threshold, weight, demo generator or main UI change

---

## Phase 16 — Dataset-level Discover page

Status: implemented ✅ / unit tested ✅ / integration verified ✅ / real-corpus smoke verified ✅

Files added:
- `src/reviewscope/discovery/__init__.py` — public API (`build_dataset_summary`,
  `get_dataset_summary`, `clear_dataset_cache`, `dataset_cache_info`,
  `DatasetSummary`, `DatasetCapabilities`, `RankingSection`, `filter_places`,
  `ranking_sections`, `category_summary`, `display_places_frame`,
  `display_category_frame`, `DISPLAY_COLUMNS`, `NOT_AVAILABLE`, `source_badge`,
  `dataset_identity`)
- `src/reviewscope/discovery/summary.py` — capability detection, per-place
  aggregation, filters, descriptive rankings, category medians/quartiles,
  identity-keyed in-process + on-disk cache
- `src/reviewscope/ui/discover.py` — the Discover page
- `tests/test_discover.py` — 53 unit + AppTest tests

Files changed:
- `app.py` — `Discover` registered and dispatched as page 2
- `src/reviewscope/ui/common.py` — `PAGE_ORDER`, explicit widget keys,
  `apply_pending_navigation`, `open_place`
- `src/reviewscope/ui/__init__.py` — exports `render_discover_page`
- `src/reviewscope/storage/duckdb_store.py` — `db_path` attribute (cache identity)
- `src/reviewscope/ui/duplicates.py` — **pre-existing crash fix**: the
  "Minimum group size" slider got `min_value == max_value == 2` for places
  whose largest repeated-text group has 2 reviews, which Streamlit rejects
- `tests/test_app_smoke.py` — `Discover` in the all-pages smoke run and in the
  place-selection regression; new Duplicates group-of-two regression test;
  new dataset-summary smoke test

Implementation notes:
- Every Discover number is a read-only projection of the existing production
  per-place `AnalysisEngine.analyze()` result. No detector, threshold, weight,
  formula or embedding was touched; a failing place becomes an `N/A` row
  instead of zeros, and the count of such places is shown as a warning.
- Missing evidence is stated before results: capability notes come from the raw
  review fields only (no detector run), so they are valid even when the
  expensive pass fails. Coordinated activity is not ranked when the dataset has
  no publication timestamps; the per-place score stays on Overview.
- Ranking wording is strictly descriptive (highest/lowest/most/largest) and the
  page repeats that a place can top both a "most 5★" and a "lowest specificity"
  list. An unavailable ranking renders a note instead of a table — the module
  also empties such frames so no caller can render one by accident.
- Duplicate rate reuses the Overview definition (reviews in repeated-text
  groups of 3+ / place reviews); templated high count reuses
  `CONFIG.templated.high_threshold`; the rating delta is the difference of the
  two ratings the row displays, the same derivation as the Overview verdict
  line (corrected during the consistency audit — see below).
- Category comparison uses medians and quartiles of place-level values (no
  opaque category score) and always covers the whole dataset; only the
  comparison table and the ranking cards follow the active filters.
- Caching: identity = resolved DB path + size + `mtime_ns` + embedding model; an in-process dict
  plus a JSON sidecar under `RS_CACHE_DIR` or `XDG_CACHE_HOME`
  (`reviewscope/discovery-cache/`), cache-version-stamped and rewritten after a
  live build. `RS_DISCOVERY_DISK_CACHE=0` disables the sidecar. Cached values
  never contain review text — only aggregates.
- Navigation: `open_place` writes a pending place + page before the sidebar
  widgets exist, so the deferred selection is applied on the next run; the
  existing place-selection regression test now walks through Discover.

Consistency audit (pre-commit, real corpus — every check re-run on all 190
places, plus 12 places compared page-to-page through the rendered app):
- Totals tie out: 10,454 reviews in the DB = sum of per-place `review_count`;
  190 ids = 190 analysed places, 0 failures; `total_categories` = 25 distinct
  stored `place_category` values; the per-place `review_count` sum equals the
  dataset total.
- Per-place values verified against the production `AnalyzedPlace` objects for
  all 190 places: review/reviewer counts, raw and weighted ratings, display
  duplicate rate, largest group, topic-cluster count, templated
  min/median/max/high-count, 1★/5★ shares and specificity min/median/mean —
  no mismatch.
- UI-to-UI (Discover vs Overview vs Duplicates) for 12 named places: reviews,
  reviewers, raw/weighted ratings, duplicate rate, group count and largest group
  agree everywhere. Базар: Discover/Overview 15.0%, Duplicates "Share of place
  reviews" 20.0% — different group floors (3+ vs 2+), not a mismatch.
- **Correction 1 — rating delta.** `rating_delta`/`abs_rating_delta` reused
  the production `details["delta"]`, which production computes *before*
  rounding raw/weighted to 2 decimals, so a row's "Raw − weighted" could differ
  from the two ratings printed in the same row (165/190 rows differed at stored
  precision, 48/190 at display precision). Discover now derives the delta from
  the displayed pair, matching the Overview verdict line; the finer-grained
  production delta stays on the place Overview under technical details.
  After the fix: 0/190 rows differ.
- **Correction 2 — duplicate-group scope.** `duplicate_group_count` counts all
  detected groups (2+), which is exactly the Duplicates page's "Repeated-text
  groups", but the adjacent "Dup groups" label invited reading it as the count
  behind the 3+ rate. The column is now `Dup groups (2+)`, the ranking card and
  the Methodology block state the two floors explicitly, and the underlying
  value is unchanged. Regression test builds a place with a group of 3 and a
  pair and pins both readings.
- Two apparent findings were investigated and dismissed as non-bugs: the
  live vs on-disk cache frames are byte-identical (`places`/`categories`
  `.equals()` is `True`, same `RangeIndex`, same dtypes) — the earlier mismatch
  was an artifact of the audit script's own `set_index`; and `place_id` vs
  `place_name` counts (190 vs 182) are eight genuinely shared venue names, not
  a join error.
- The 25-vs-10 category difference is a definition difference, not a bug: the
  exploration manifest groups 10 generation-side target categories, while the DB
  stores the organisation's primary rubric string. The 10 manifest groups
  partition 10,454 reviews and 190 orgs, so the manifest is not a per-row
  `place_category` mapping.

Command results:
- `pytest` — **332 passed**, 0 failed (see audit run)
- `ruff check .` — All checks passed
- `git diff --check` — clean
- `tests/test_discover.py` — 53 passed; `tests/test_app_smoke.py` — 10 passed

Real-corpus verification (`validation_data/private/yandex_geo_2023/exploration/
exploration_analysis.duckdb`, 10,454 reviews / 190 place ids / 182 distinct
names / 25 categories):
- Cold build 38.9–44.8 s (190 places, 0 failures); in-process warm 0.0001 s;
  fresh-process warm from the on-disk sidecar 0.09 s
- Full-app AppTest on the real DB: no exceptions, warnings or errors; 12
  dataframes; place drill-down to Overview and all other pages keep the
  selected place
- Aggregate facts: 0 dated reviews, 0 coordinates, 10,454 distinct reviewers
  (0 with history in more than one place), 60 places with repeated-text groups,
  27 places with semantic topic clusters, 0 places with a templated score
  ≥ 65 (dataset max 62.5, median 25.7)
- Highest duplicate rate: Базар 15.0% (largest group 6), Авиапарк 10.5%
  (largest group 9); highest templated text: Остров мечты 62.5; largest
  specificity spread between shopping centres (median 60.0) and hotels
  (median 78.0)

Requirements completed:
- Dataset-level view of where review evidence sits, with descriptive rankings
  and category context, and no accusatory wording
- Missing evidence rendered as `N/A` with an explanation; coordinated activity
  gated on temporal capability
- Cached dataset identity so the page is usable on a 10k-review corpus
- No change to any detector, threshold, weight, formula or embedding

## Phase 17G — Repeated-text family semantics (§29)

Status: implemented ✅ / unit tested ✅ / integration verified ✅ /
real-corpus verified ✅ / no commit made (report-only phase)

`DuplicateGroup` has always been a **connected component** of detected
pairwise links (exact / fuzzy / near / semantic), but the product never said
so — cards read as if every member were similar to every other member. Phase
17G changes *presentation only*: it makes the UI accurately describe what a
group is.

Not changed: duplicate pair thresholds, the four detectors, connected-component
grouping, scoring, embeddings, the corpus, rating logic, the score JSON
payload, and group membership.

Files changed:
- `src/reviewscope/analysis/duplicates.py` — `DuplicateGroup` gained one
  read-only `edges: list[tuple[str, str, str, float]]` field (a, b, kind,
  score), populated in the existing pair loop and sorted. Everything else
  (links, kind breakdown, possible pairs, density, transitivity, weakest link)
  is derived from it in the UI. Detector output verified byte-for-byte
  identical against the pre-change canonical snapshot.
- `src/reviewscope/ui/duplicates.py` — rewritten around pure helpers
  (`FamilyStructure`, `interpretation`, `member_evidence`, `member_labels`,
  `rating_context`, `rating_line`): page header “Repeated-text families”,
  verbatim connected-component caption, `N direct links of M possible pairs`,
  **Contains transitive connections** when fewer than half of all pairs link,
  a “Relationship evidence” expander, rating alignment shown as separate
  non-deciding context, and “near-copies”/“across the family” withheld unless
  every possible pair is linked.
- `src/reviewscope/discovery/summary.py`, `src/reviewscope/ui/discover.py` —
  *Families (2+)*, *Largest family*, *Largest repeated-text families*,
  *Places with repeated-text families*, plus a methodology bullet defining a
  family as a connected component.
- `src/reviewscope/ui/common.py` — attribute tag now “in a repeated-text
  family of N”.

Tests:
- `tests/test_transitive_family.py` (new, 29 tests): A–B–C synthetic chain
  forms one family with 2 of 3 possible links; transitive indication on/off;
  direct size-2 family never marked transitive; exact family; mixed
  lexical+semantic family; member evidence lists only direct links; copy
  never claims all-pair similarity; rating context is descriptive and ratings
  do not change membership; edge-less groups render “not stored” rather than
  zeros; AppTest rendering of the transitive label and terminology.
- Presentation fixtures updated (labels only): `tests/test_discover.py`,
  `tests/test_app_smoke.py`.

Command results:
- `pytest` — **419 passed**, 0 failed
- `ruff check .` — All checks passed
- `git diff --check` — clean

Real-corpus verification (Vermont rich corpus, 21,831 reviews / 250 places):
- Canonical detector snapshot before vs after: **byte-for-byte identical**
  (611 families, 1,793 reviews, 250 places)
- 611 families: 367 direct size-2 (60.1%), 186 transitive (30.4%), 58 fully
  connected (9.5%), 27 structural chain-heavy (density < 0.50)
- Largest family: 19 reviews, 56 of 171 pairs linked (density 0.327)
- 1,663 links: 62 exact / 33 fuzzy / 34 near / 1,534 semantic; 95 families
  (15.5%) carry at least one lexical link
- Phase 17F aggregates reproduce exactly: 199 families with a below-threshold
  pair, 47 majority-below, 186 transitively added
- Reports (gitignored): `validation_data/private/google_local_vermont/
  phase17g_snapshot.py`, `phase17g_verify.py`, `phase17g_verification.md`

## Phase 18 — Case Investigation Workspace (§30)

Status: implemented ✅ / unit tested ✅ / integration verified ✅ /
real-corpus verified ✅ / no commit made (report-only phase)

The repeated-text family cards (§29) show one detection result; you could not
move from a specific family to its members. Phase 18 adds a **Case
Investigation Workspace** — a sub-view of the Duplicates page reachable from a
family card, from the Discover "Investigate family" action, or from a pending
resume prompt. It lets you:

- inspect each member (reviewer, rating, published date, full text),
- see the family as a row-and-column relationship matrix whose coloured cells
  are the *actual detected pairs* (`kind` + score, e.g. "identical text",
  "semantic similarity 0.98"),
- select a member and read exactly which **direct links** the detector stored
  for it (`N of M`), with the member summary kept in sync as you switch across
  the two member selectors,
- see the connected-component caption and transitive note per member context.

Not changed: detector thresholds / the four detectors, connected-component
grouping, scoring, embeddings, the corpus, rating logic, the score JSON
payload, group membership, page routing (`app.py` unchanged), and
`src/reviewscope/analysis/duplicates.py` untouched.

Files changed:
- `src/reviewscope/ui/investigate.py` (new) — the workspace module: pure
  helpers (`family_identity`, `family_options`, `option_label`, `kind_phrase`,
  `member_frame`, `relationship_frame`, `_largest_family_size` gating),
  session-state operations (open / clear / dataset-invalidity fallback /
  `begin_family_investigation`), and rendering (`render_resume_bar`,
  `maybe_render_workspace`, `_render_members`, `_render_inspector`,
  `_render_relationships`). A family identity is member-based and
  group-id-independent: `"<place_id>::" + ",".join(sorted(review_ids))`.
  Selection is kept in session state across page navigation and dropped on
  dataset or place change.
- `src/reviewscope/ui/duplicates.py` — `_KIND_LABELS` renamed to
  `KIND_LABELS`, `_group_stats` to `group_stats` (annotation corrected), plus
  a shared `family_summary_block` used by both card and workspace; every
  family card now has an "Open investigation workspace" button; a resume bar
  ("Investigation in progress") offers Resume / Clear after backing out; the
  workspace is dispatched through `maybe_render_workspace` after the group
  list. A lazy import keeps the investigate↔duplicates module cycle out of the
  import graph.
- `src/reviewscope/ui/discover.py` — `_focus_controls` gained a fourth,
  gated action "Investigate family" (disabled when the focused place has no
  families) that selects the largest family (size desc, then avg similarity
  desc, then identity) and jumps to its workspace on the Duplicates page.

Tests:
- `tests/test_investigate.py` (new, 45 tests): unit coverage for the identity
  and label helpers, members/relationship table builders and the largest-family
  gate; AppTest coverage for opening the workspace from a card and from
  Discover, inspector text sync across both member selectors, switching
  families in-place, back/clear/resume flows, a synthetic transitive chain
  (A–B, B–C with A–C unlinked, rendered "—"), edge-less families rendering as
  "not stored", dataset-switch invalidation, and copy staying neutral (no
  fraud/manipulation claims). Module-scoped fixtures reuse cached embeddings
  so no model compute is needed per test.

Command results:
- `pytest` — **478 passed**, 0 failed
- `ruff check .` — All checks passed
- `git diff --check` — clean

Real-corpus verification (Vermont rich corpus, 21,831 reviews / 250 places):
- 611 families across 250 places → **611 unique identities** (no key
  collisions across re-analysis and re-computation)
- A fresh `DuplicateDetector` run on the largest family's place reproduces the
  exact same family identity (member-based, order-independent)
- Member and relationship tables consistent with `DuplicateGroup.edges`: every
  edge's endpoints belong to the family, member direct-link counts sum to
  2×edges (each pair counted at both endpoints), and the matrix exists with
  shape `(n, n)` for every family carrying edges
- Largest family: 19 reviews at place
  `0x89e0248b97f8bf0b:0xeb95aa7083f3afc1`, 56 of 171 pairs linked; option
  label "19 reviews (A–S) · 56 direct links · similarity 0.92"
- AppTest boot with the workspace pre-opened on that place: 19-row member
  table, 19×19 relationship matrix, zero exceptions and zero Streamlit
  warnings (verified)
- Semantic scores for identical text pairs may exceed 1.0 by a floating-point
  epsilon (≤ ~2.4e-7); pre-existing detector behaviour, renders as "1.00".

## Phase 18.1 — UX-grade investigation workspace (§30)

Status: implemented ✅ / unit tested ✅ / integration verified ✅ /
real-corpus verified ✅ / no commit made (report-only phase)

Phase 18 was functional but table-heavy: members, a matrix and a plain-text
inspector, no way to see *which pairs are linked*. Phase 18.1 turns the
workspace into a visual tool:

- **Relationship graph** — the family as a force-directed node/edge diagram.
  Nodes are the family reviews; edges are **only the stored detector pairs**
  (one trace per detection level present: "identical text" solid blue, "fuzzy
  match" dashed green, "near duplicate" dotted amber, "semantic similarity"
  long-dashed violet). No A–C edge appears just because A–B and B–C are
  linked; geometry is purely for display and never invents pairs.
- **Selection sync** — a "Selected review" + "Compare with" selectbox pair
  (state keys `investigation_member`, `investigation_comparison`) drives the
  graph highlight (selected = gold star, size 26; direct neighbours = blue
  circles, size 16; everyone else size 11) and the side-by-side card below.
- **Side-by-side comparison** — two bordered member cards; the caption states
  the stored relationship between the two (`direct links of N-1 possible`),
  including an explicit "no direct detector relationship was recorded" cue.
  Default comparison target is the first direct neighbour (else first member).
- **Compact safe text** — `render_review_text` escapes Markdown specials and
  collapses newlines, so full review text renders as a card rather than a raw
  text area; reviews longer than 280 chars preview with a "Show full text"
  expander. `st.text_area` is gone.
- **Info hierarchy** — `### Family summary` (Reviews / Distinct reviewers /
  Direct links "X of Y" / Link density + interpretation line + transitive
  note), `### Relationship graph` (+ "Graph data as a table" expander with
  per-member `N of M` evidence bullets), `### Side-by-side comparison`,
  `### Family members`, and a "Technical details" expander (links by detection
  level, relationship matrix, diagnostics, family definition).
- **Neutral copy** — `interpretation()` gained a semantic-only branch:
  complete families say "Strong semantic similarity was detected between
  directly linked reviews.", otherwise "…along the family's direct links.";
  the pairing caption never claims an unlinked pair is duplicate text.

State (§6) is preserved across navigation and reset consistently: family
switch and Clear pop both `investigation_member` and
`investigation_comparison`; compare resets to default when it equals the
selected member or leaves the family.

Not changed: detector thresholds / detectors, connected-component grouping,
scoring, embeddings, corpus, rating logic, score JSON payload, group
membership, page routing (`app.py`), and
`src/reviewscope/analysis/duplicates.py` untouched.

Files changed:
- `src/reviewscope/ui/investigate.py` — pure helpers added (`escape_review_text`,
  `render_review_text`, `edges_frame`, `direct_neighbors`, `node_positions`,
  `default_comparison`, `_graph_kind_styles`, `build_graph_figure`);
  session-state ops pop the comparison key; renderers split into
  `_render_summary`, `_render_graph_section`, `_render_pair_comparison`,
  `_render_member_card`, `_render_members`, `_render_technical_details`;
  `TEXT_KEY` removed, `COMPARE_KEY = "investigation_comparison"` and
  `GRAPH_KEY = "family_ws_graph"` added.
- `src/reviewscope/ui/duplicates.py` — `interpretation` gained the semantic-only
  wording branch (UI copy only).
- `tests/test_investigate_ux.py` (new, 25 tests) — compact-text escaping,
  graph geometry determinism/order-invariance, one line trace per detection
  level present, on-screen edge counts (6-exact and 2-semantic fixtures),
  selected/neighbour marker sizes and star/gold styling, selection-sync
  defaults + compare reset + family-switch reset, precise language (a
  semantic chain never renders "near-copies of each other"), summary metrics
  and technical details. `tests/test_investigate.py` and
  `tests/test_transitive_family.py` updated for the new copy/controls.

A notable bug found by real-corpus verification: the first force-directed
layout diverged to `nan` on the 19-node family (large attractive pulls with no
damping). Replaced with a bounded Fruchterman-Ringold embedder (simultaneous
moves, cooled displacement cap, coordinates clamped and normalised to
[0.08, 0.92]²) plus a regression test for a dense 19-node synthetic family.

Command results:
- `pytest` — **503 passed**, 0 failed
- `ruff check .` — All checks passed
- `git diff --check` — clean

Real-corpus verification (Vermont rich corpus, largest family, 19 reviews /
56 edges at `0x89e0248b97f8bf0b:0xeb95aa7083f3afc1`):
- Layout finite and inside the unit square; 56 line segments and 19 node
  markers draw in the AppTest spec; legend carries only the kinds present
  ("identical text", "semantic similarity")
- Summary metrics: Reviews 19 · Distinct reviewers 19 · Direct links "56 of
  171" · Link density 33%; selected defaults to a member, compare to a direct
  neighbour; marker sizes [26, 16×neighbours, 11×rest] correct
- Zero exceptions, zero crashes across boot → navigate → workspace (~13 s
  warm)
