"""Explicit public-demo (read-only) configuration for ReviewScope.

The public demo is a *deployment mode*, not a different product: the local
full-featured application keeps its dataset picker, annotation tooling and
writable stores, while the public instance runs against one packaged synthetic
dataset and never writes to disk because of visitor activity.

The mode is opt-in and explicit. It is resolved from (first match wins):

1. the ``RS_PUBLIC_DEMO`` environment variable (local runs, containers, any
   host that can set environment variables);
2. the ``public_demo`` entry of ``.streamlit/secrets.toml`` (Streamlit
   Community Cloud exposes only secrets, see docs/DEPLOYMENT.md).

Nothing in this module reads review data or performs I/O at import time.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variable that turns on public-demo mode.
ENV_FLAG = "RS_PUBLIC_DEMO"
#: Environment variable that overrides the packaged dataset location.
ENV_DB_PATH = "RS_DEMO_DB_PATH"
#: Secret name used on hosts without environment-variable support.
SECRET_FLAG = "public_demo"
#: Secret name used to override the packaged dataset location.
SECRET_DB_PATH = "demo_db_path"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Packaged synthetic dataset, resolved relative to the repository root.
DEFAULT_DEMO_DB = Path("demo") / "reviewscope_demo.duckdb"

#: Fixed dataset label shown instead of a path field in public-demo mode.
DEMO_DATASET_LABEL = "Public demo dataset (synthetic)"

#: Disclaimer rendered on every page in public-demo mode.
SYNTHETIC_DATA_NOTE = (
    "**Public demo — synthetic data.** All places, reviewers and review texts "
    "are computer-generated examples; they do not describe real people, "
    "businesses or events. Results are illustrative signals, not findings about "
    "anyone, and never proof of fraud, fake reviews or AI authorship."
)

#: Short sidebar variant of the disclaimer.
SYNTHETIC_DATA_BADGE = "Synthetic demo data — no real people or businesses"


def _as_flag(value: object) -> bool:
    return str(value).strip().lower() in _TRUTHY


def _secret(name: str):
    """Read a Streamlit secret, or ``None`` when secrets are unavailable.

    ``st.secrets`` raises when no ``secrets.toml`` exists, and importing
    Streamlit is pointless outside an app run, so every failure collapses to
    "not configured".
    """
    try:
        import streamlit as st

        return st.secrets.get(name)
    except Exception:  # noqa: BLE001 - secrets are optional by design
        return None


def public_demo_enabled() -> bool:
    """True when this process must run as the read-only public demo."""
    env = os.environ.get(ENV_FLAG)
    if env is not None:
        return _as_flag(env)
    return _as_flag(_secret(SECRET_FLAG) or False)


def configure_public_demo() -> None:
    """Apply process-wide settings required by public-demo mode.

    Idempotent; called once per app run before any page renders. The only
    effect today is disabling the on-disk discovery sidecar, which would
    otherwise let a visitor's first request write under ``~/.cache``. The
    value is forced (not defaulted) so a stray host-level setting cannot
    re-enable visitor-triggered writes.
    """
    os.environ["RS_DISCOVERY_DISK_CACHE"] = "0"


def repo_root() -> Path:
    """Repository root (three levels above ``reviewscope/public_demo.py``)."""
    return Path(__file__).resolve().parents[2]


def public_demo_db_path() -> Path:
    """Location of the packaged synthetic dataset for public-demo mode."""
    env = os.environ.get(ENV_DB_PATH)
    if env:
        return Path(env)
    secret = _secret(SECRET_DB_PATH)
    if secret:
        return Path(str(secret))
    candidates = (repo_root() / DEFAULT_DEMO_DB, Path.cwd() / DEFAULT_DEMO_DB)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def missing_embedding_keys(store, reviews: list) -> list[tuple[str, str, str]]:
    """Cache keys for ``reviews`` that are not persisted in ``store``.

    Used as a startup preflight: the public demo ships precomputed embeddings
    so the model (and PyTorch) never has to load, and an incomplete artifact
    must fail loudly instead of degrading the analysis silently.
    """
    from reviewscope.config import CONFIG

    model_name = CONFIG.embedding.model_name
    keys = [(r.review_id, r.fingerprint(), model_name) for r in reviews]
    if not keys:
        return []
    cached = store.cached_embedding_keys()
    return [k for k in keys if k not in cached]
