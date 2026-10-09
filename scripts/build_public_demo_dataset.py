#!/usr/bin/env python3
"""Build (or verify) the packaged public-demo dataset for ReviewScope (Phase 19).

The public demo ships one synthetic DuckDB artifact so a fresh cloud instance
starts without downloading models, configuring APIs or having local developer
files. This script is the *maintainer-side* reproducible build:

1. regenerate the synthetic demo corpus (deterministic ``SEED``);
2. ingest it into a fresh DuckDB file;
3. precompute every embedding with the configured model;
4. write ``demo/manifest.json`` (counts, model, file and content digests).

Steps 1-2 are model-free. Step 3 needs the ``embeddings`` extra
(``pip install -e ".[embeddings]"`` plus the CPU torch wheel) and a one-time
model download. The deployed application never runs step 3: it reads the
precomputed vectors out of the artifact (see docs/DEPLOYMENT.md), which is why
``--verify`` below must pass without PyTorch installed.

Usage::

    python scripts/build_public_demo_dataset.py            # build
    python scripts/build_public_demo_dataset.py --verify   # check artifact

Exit code 0 means the artifact is present, complete and matches the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from reviewscope.config import CONFIG  # noqa: E402
from reviewscope.ingestion.csv_adapter import CSVAdapter  # noqa: E402
from reviewscope.public_demo import missing_embedding_keys  # noqa: E402
from reviewscope.storage import DuckDBStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "demo" / "reviewscope_demo.duckdb"
MANIFEST_PATH = REPO_ROOT / "demo" / "manifest.json"
DEMO_CSV = REPO_ROOT / "data" / "demo_reviews.csv"
DEMO_META = REPO_ROOT / "data" / "demo_dataset_meta.json"
GENERATOR = "scripts/generate_demo_data.py"

#: The generator's seed, mirrored here so the manifest is self-describing.
GENERATOR_SEED = 20260901


# ---------------------------------------------------------------------------
# Helpers (model-free)
# ---------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_digest(store: DuckDBStore) -> str:
    """Digest of review rows plus every cached embedding value.

    Byte-level file digests change between DuckDB builds; this content digest
    does not, so it is the meaningful reproducibility check.
    """
    digest = hashlib.sha256()
    for row in store.connection().execute(
        "SELECT review_id, text_hash, model_name, embedding "
        "FROM embeddings_cache ORDER BY review_id, text_hash, model_name"
    ).fetchall():
        review_id, text_hash, model_name, embedding = row
        digest.update(str(review_id).encode())
        digest.update(b"\0")
        digest.update(str(text_hash).encode())
        digest.update(b"\0")
        digest.update(str(model_name).encode())
        digest.update(b"\0")
        digest.update(np.asarray(embedding, dtype=np.float32).tobytes())
    return digest.hexdigest()


def inspect(db_path: Path) -> dict:
    """Read-only summary of an artifact: counts, missing embeddings, digests."""
    if not db_path.exists():
        return {"error": f"artifact not found: {db_path}"}
    with DuckDBStore(db_path=db_path, read_only=True) as store:
        reviews = store.fetch_reviews()
        missing = missing_embedding_keys(store, reviews)
        return {
            "reviews": store.review_count(),
            "places": len(store.list_places()),
            "reviewers": store.distinct_reviewer_count(),
            "embedding_rows": store.cached_embedding_count(),
            "embedding_model": CONFIG.embedding.model_name,
            "missing_embeddings": len(missing),
            "content_sha256": content_digest(store),
        }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(output: Path = DEFAULT_OUTPUT, *, generate: bool = True) -> dict:
    """Regenerate, ingest and embed the packaged dataset; returns the manifest."""
    if generate:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "generate_demo_data", REPO_ROOT / GENERATOR
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules["generate_demo_data"] = module
        spec.loader.exec_module(module)
        if module.main() != 0:
            raise SystemExit("demo data generation failed")

    loaded = CSVAdapter().load(DEMO_CSV)
    if loaded.report.skipped:
        raise SystemExit(f"demo CSV lost {loaded.report.skipped} rows")
    reviews = loaded.reviews

    output.parent.mkdir(parents=True, exist_ok=True)
    for stale in (output, Path(str(output) + ".wal")):
        if stale.exists():
            stale.unlink()

    store = DuckDBStore(db_path=output, read_only=False)
    try:
        written = store.ingest(reviews)
        # Step 3: precompute embeddings (needs the `embeddings` extra).
        from reviewscope.embeddings.cache import EmbeddingCache

        EmbeddingCache(store).embed_reviews(reviews)
    finally:
        store.close()

    info = inspect(output)
    if info.get("reviews") != written:
        raise SystemExit(f"ingest wrote {written}, artifact holds {info.get('reviews')}")
    if info.get("missing_embeddings"):
        raise SystemExit(
            f"{info['missing_embeddings']} embeddings missing after the build"
        )

    meta_copy = output.parent / "demo_dataset_meta.json"
    if DEMO_META.exists():
        shutil.copyfile(DEMO_META, meta_copy)

    manifest = {
        "artifact": output.name,
        "source": "synthetic (generated, no real personal data)",
        "generator": {"script": GENERATOR, "seed": GENERATOR_SEED},
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": file_sha256(output),
        "size_bytes": output.stat().st_size,
        **info,
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify(output: Path = DEFAULT_OUTPUT) -> list[str]:
    """Return a list of problems (empty when the artifact is deploy-ready)."""
    if not output.exists():
        return [f"artifact not found: {output}"]
    if not MANIFEST_PATH.exists():
        return [f"manifest not found: {MANIFEST_PATH}"]
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []

    info = inspect(output)
    if "error" in info:
        return [str(info["error"])]
    for field in ("reviews", "places", "reviewers", "embedding_model"):
        if info[field] != manifest.get(field):
            problems.append(
                f"{field}: artifact has {info[field]!r}, manifest says {manifest.get(field)!r}"
            )
    if info["missing_embeddings"]:
        problems.append(f"{info['missing_embeddings']} embeddings missing")
    if info["content_sha256"] != manifest.get("content_sha256"):
        problems.append(
            "content digest mismatch (artifact and manifest were built from "
            "different data or embeddings)"
        )
    if file_sha256(output) != manifest.get("sha256"):
        problems.append("file digest mismatch (artifact changed on disk)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"artifact path (default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify an existing artifact against demo/manifest.json (no model needed)",
    )
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        help="reuse data/demo_reviews.csv instead of regenerating it",
    )
    args = parser.parse_args(argv)

    if args.verify:
        problems = verify(args.output)
        if problems:
            for problem in problems:
                print(f"FAIL: {problem}")
            return 1
        print(f"OK: {args.output} matches {MANIFEST_PATH.name}")
        return 0

    manifest = build(args.output, generate=not args.skip_generate)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
