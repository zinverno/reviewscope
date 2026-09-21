# ReviewScope — Product UX Pass (Final Report)

Scope: a focused information-architecture and presentation overhaul of the
Streamlit app while keeping every detector, scoring formula, threshold,
weight, embedding behavior, clustering step, and the validation framework
**analytically unchanged**. All changes are presentation-only.

## Implementation summary

- Added a shared rendering/formatting layer in `src/reviewscope/ui/common.py`:
  friendly signal/counter-signal wording (no new facts), honest baseline and
  ratio phrasing, neutral empty states (`info_state`), human duration/percent
  formatting, `N/A / insufficient history` value handling, coordinate-missing
  detection that treats DuckDB `nan` as missing (previously only `None` was
  checked), technical-details expanders, and per-review attribute tags.
- Reworked all seven pages and the sidebar to a progressive-disclosure pattern:
  headline numbers / verdict first, then evidence, with raw technical metrics
  behind expanders. Terminology constrained to *suspicious activity*,
  *coordinated activity signals*, *templated text*, *synthetic-like text*,
  *anomaly*, *confidence*, *evidence* — never fraud/“AI-generated” claims.
- Fixed a real (pre-existing) presentation bug: coordinates stored as `nan`
  (DuckDB missing floats) previously bypassed `is not None` checks, so the
  Reviewed Places map could plot invalid points instead of showing its empty
  state. Now handled in both Reviewed Places and Data Quality.
- Rewrote the AppTest smoke suite to run against a **copy** of the production
  DB (unaffected by the running Streamlit process's lock) and added synthetic
  state tests (clean/quiet place, missing coordinates, insufficient reviewer
  history). Also fixed the navigation pattern: AppTest widgets are single-use,
  so site navigation must re-fetch the page radio after every run; the old
  “all pages run” test silently stayed on the Overview page.

## UX changes by page

- **Overview** — place name/category header; 2×3 metric grid (Raw rating,
  Weighted rating, Reviews — labels preserved per existing tests; plus
  Reviewers, Suspicious activity badge, Duplicate rate); weighted-vs-raw
  verdict in plain language; “Main contributing signals” + “Counter-signals”
  in friendly wording; “Coordinated activity” score/confidence block with
  why/counter list; three charts with per-chart empty states; keywords line;
  technical score components behind expanders.
- **Topics** — topic cards (border containers) with honest `Topic N` labels +
  actual representative phrases (never invented names), review count, share of
  selection, average rating, date range, and representative-review expander.
  Clear message when clusters are empty or the selection is too small.
- **Duplicates** — two-group minimum; top summary (groups / reviews involved /
  share); filters for group size, average similarity, date concentration, and
  detection levels; sortable; group cards with a neutral interpretation line
  (“repeated review pattern”, “high textual similarity”), review-text expander,
  and an explicit detection breakdown that labels fuzzy/near/semantic counts as
  *pairs* and identical-text as *reviews* (never presenting pair counts as
  review counts).
- **Anomalies** — volume bursts and rating shifts as separate card sections;
  every card shows event type, date, severity, observed volume, expected
  baseline, and honest ratio (near-zero baselines show
  “Expected baseline: <1 review/day” / “ratio not meaningful” instead of a
  misleading 30×); rating mix; neutral evidence/counter-signals; affected
  reviews in an expander with lightweight attribute tags (rating, weight,
  repeated-text group, templated text) and “+N more” paging; technical values
  (z-score, MAD, multiplier, JSD) in expanders. Window size read from
  `CONFIG.rating_anomaly.event_window_days` rather than hard-coded.
- **Reviewers** — 4-metric summary, “Most active reviewers” leaderboard (top
  20 with share-of-reviews), profile card with place/dataset context, and
  profile evidence from the existing `category_experience_score`,
  `local_familiarity_score`, `reviewer_relevance_score` functions (missing
  metadata rendered as `N/A / insufficient history`), plus aggregate technical
  metrics behind an expander.
- **Reviewed Places** — retains the explicit privacy disclaimer (timestamps ≠
  verified movement; no home/work/route inference), adaptive map zoom for
  1 / 2–5 / many locations, mapped-location summary line, and a friendly
  empty state when the selection has no coordinates.
- **Data Quality** — “complete records” framing (all key fields present and
  parseable) instead of “valid”; checks grouped by importance (Important /
  Moderate / Minor), each with count, share, and its impact on product views;
  still never calls stored reviews invalid.

## Analytical ambiguity discovered — NOT changed

- `exact_count` in duplicate groups counts *reviews* while `fuzzy_count`,
  `near_count`, `semantic_count` count *pairs*. This asymmetry originates in
  the analysis layer; we did not alter the detector. The UI now labels each
  count honestly (pairs vs reviews) and explains the difference.
- Burst events can report a huge volume multiplier against a ~0 baseline
  (e.g. 30 vs 0.0/day). We keep the detector as-is; the UI renders an honest
  “baseline <1 review/day” phrase instead of “30×”.
- The listing below changed no analysis module (`src/reviewscope/analysis/*`
  untouched); detector outputs are the source of truth for every number shown.

## Files changed

- `app.py` — sidebar grouping (filters in an expander), reviewer-restrict
  toggle help text, divider, page-radio help, and a bottom disclaimer that the
  app does not assert fraud/intent/verified movement.
- `src/reviewscope/ui/common.py` — new helpers (see summary) incl. the
  `nan`-aware `has_coordinates()`.
- `src/reviewscope/ui/overview.py`, `topics.py`, `duplicates.py`,
  `anomalies.py`, `reviewers.py`, `reviewed_places.py`, `data_quality.py` —
  page redescriptions described above.
- `tests/test_app_smoke.py` — lock-free DB-copy harness, real page-navigation
  assertions, and synthetic state tests.

Not touched: `app_labeling.py`, `src/reviewscope/validation/*`, all
`src/reviewscope/analysis/*` modules, demo generator, models.

## Verification

- **Tests**: `uv run pytest` → **268 passed, 0 failed** (~8.5 min; includes
  the embedding-model integration suite).
- **Lint**: `uv run ruff check .` → all checks passed (line-length 100, select
  E/F/W/I/UP/B, E501 ignored — per `pyproject.toml`).
- **White space**: `git diff --check` → clean.
- **AppTest/smoke**: 7/7 pass — boot, real navigation across all 7 pages (with
  per-page header assertions), rating metrics, explainability text, clean-state
  pages, missing-coordinates states, `N/A / insufficient history`.
- **Launch**: `streamlit run app.py` is already running (PID 2945568) and
  holds the DB lock; Streamlit auto-reloads the script on change, and the
  AppTest boots (same script, headless) confirm the app runs. Browser
  automation is unavailable in this environment, so visual/HTTP-level
  verification is limited to the AppTest substitute (recorded as a known
  limitation per SPEC.md §39).

## Known UX limitations

- Empty/edge states render as `streamlit.info` boxes; a dedicated styled empty
  component (icon + softer copy) needs a frontend asset and was out of scope.
- Filters apply to review-level views; summary metrics stay place-level by
  design (they come from the engine and are the source of truth) — noted via a
  caption when filters are active.
- The duplicate “date concentration” and similarity sliders are presentational
  filters over existing detector output, not new detectors.
- Reviewer profiles run the existing score functions over full dataset history;
  very small histories legitimately render as `N/A / insufficient history`.

## git status

```
 M app.py
 M src/reviewscope/ui/anomalies.py
 M src/reviewscope/ui/common.py
 M src/reviewscope/ui/data_quality.py
 M src/reviewscope/ui/duplicates.py
 M src/reviewscope/ui/overview.py
 M src/reviewscope/ui/reviewed_places.py
 M src/reviewscope/ui/reviewers.py
 M src/reviewscope/ui/topics.py
 M tests/test_app_smoke.py
?? docs/UX.md           (this report)
```

No commits or pushes were made.