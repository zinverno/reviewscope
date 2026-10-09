# Deployment Guide

## Overview

ReviewScope ships as a public read-only demo for Streamlit Community Cloud and as a local full-featured application. This guide covers the deployment configuration required for a public beta on Streamlit Community Cloud.

### Key constraints

- Public demo runs **without** `sentence-transformers`/PyTorch (embeddings are precomputed).
- The public dataset is fixed (no path picker), synthetic, and treated as untrusted input.
- No visitor-triggered filesystem writes in public-demo mode. Discovery sidecar is forced off.
- Annotation tooling (`app_labeling.py`) refuses to start in public-demo mode.
- Connections to the bundled DuckDB are opened in `read_only=True`.

## Python version

Streamlit Community Cloud supports Python 3.9–3.13 and defaults to 3.12. Select **Python 3.12** in "Advanced settings" when creating the app. ReviewScope requires `>=3.12` (see `pyproject.toml`).

## Entry point

- Main app: `app.py`
- Public demo artifact: `demo/reviewscope_demo.duckdb` (committed)
- Demo manifest: `demo/manifest.json`

## Dependencies

Use `requirements.txt` for Community Cloud. It contains only runtime dependencies and an editable install:

```txt
-e .
streamlit==1.64.0
pandas==3.0.6
numpy==2.5.3
duckdb==1.5.5
scikit-learn==1.9.1
scipy==1.18.1
hdbscan==0.8.44
rapidfuzz==3.14.6
datasketch==2.0.0
plotly==7.1.0
pydantic==2.13.5
```

The `embeddings` extra (which pulls in `sentence-transformers` and PyTorch) is **not** installed in the public demo. Precomputed embeddings in the packaged dataset remove the need for model loading entirely. If missing, the app fails loudly with a clear error pointing to this guide.

For local full-featured work: `pip install -e ".[dev,embeddings]"`.

## Configuration

### `.streamlit/config.toml` (committed)

```toml
[server]
headless = true
maxUploadSize = 1

[browser]
gatherUsageStats = false
```

### Secrets (Community Cloud)

Define these in the Streamlit app secrets (`Settings > Secrets`):

| Key | Type | Purpose |
|---|---|---|
| `public_demo` | `1` or `true` | Enables public-demo mode. Required for the read-only public instance. |
| `demo_db_path` | path (optional) | Override for the packaged dataset. Usually not needed; defaults to `demo/reviewscope_demo.duckdb`. |

When enabled, the app forces `RS_DISCOVERY_DISK_CACHE=0` and never renders a dataset path input. `SYNTHETIC_DATA_NOTE` is shown on every page.

## Public-demo mode behavior

- Reads only from the bundled synthetic DuckDB (`read_only=True`). Missing files raise `FileNotFoundError` and are never created.
- `store_cached_embeddings()` is a no-op in read-only mode (returns 0) — no write attempts.
- Preflight checks that all reviews have persisted embedding keys (`cached_embedding_keys()`) and fails with deployment instructions if incomplete.
- Discovery disk cache is disabled (process-enforced). Measured: 0 disk writes in demo runs.
- `app_labeling.py` exits with an explanatory error when `public_demo_enabled()` is True.
- All review text and reviewer identifiers are escaped via a shared helper before `st.markdown` (defends against Markdown injection while preserving readability).
- Dataset label is fixed to "Public demo dataset (synthetic)" and carries a synthetic-data badge/caption.

## Deployment steps (Community Cloud)

1. Fork or push this repository to GitHub (branch with the release candidate).
2. Go to [share.streamlit.io](https://share.streamlit.io/) and create a new app.
3. Select the GitHub repo and branch.
4. Set main file path to `app.py`.
5. In Advanced settings, choose **Python 3.12**.
6. Add secrets: `public_demo = "1"` (and optionally `demo_db_path` only if using a custom location).
7. Deploy (no build hook/custom packages). Dependencies come from `requirements.txt`.

No network calls to model endpoints occur in public-demo mode; the artifact is self-contained.

## Local verification

```bash
# runtime deps only (demo mode)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
RS_PUBLIC_DEMO=1 streamlit run app.py

# full test suite with embeddings/dev
pip install -e ".[dev,embeddings]"
pytest
ruff check .
```

The packaged artifact's integrity is verified by `scripts/build_public_demo_dataset.py verify` (committed `manifest.json` + `file_sha256`).

## Security/Privacy

- Synthetic dataset only (`SEED = 20260901`). No real PII.
- Read-only DB, no upload path exposed in public mode, annotation paths disabled.
- Escaping applied at render time for untrusted text.

## Known limits

- `get_store`/`get_engine` use `lru_cache(maxsize=1)` and never close an evicted connection (documented pre-existing behavior; not modified in this phase). 
- CPU-only runtime; embedding recomputation requires the `embeddings` extra + model weights (local only).
- Community Cloud memory is constrained (single-process). See measurements below.
