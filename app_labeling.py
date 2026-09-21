#!/usr/bin/env python3
"""ReviewScope — blind human-labeling app (Phase 15).

This app is the only human-facing annotation surface. It is **blind by
construction**:

* It loads ONLY the review rows and the deterministic sample selection.
* It never loads, receives, or renders any ReviewScope output — no templated
  score, no specificity score, no duplicate-group predictions, no sampling
  stratum labels. Nothing in the UI can reveal a detector preference.
* The annotator sees the review text plus neutral context (place, category,
  rating, date) and records independent verdicts for the templated / template
  group, specificity and duplicate dimensions.

Persistence is the ``validation/annotation.py`` ``AnnotationStore``: every
verdict is an UPSERT keyed by ``review_id`` in a DuckDB file
(``<validation-dir>/annotations.duckdb``), so interrupted batches resume
exactly where they stopped. The labeling batch is only considered finalised
when the whole selection is annotated; until then nothing here reveals scores.

Run it like the main app::

    python -m streamlit run app_labeling.py

Configuration (sidebar or ``RS_VALIDATION_DIR`` env var): the validation
directory produced by ``scripts/validation_sample.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="ReviewScope — Blind Labeling", layout="wide")

from reviewscope.storage import DuckDBStore  # noqa: E402
from reviewscope.validation.annotation import AnnotationStore  # noqa: E402
from reviewscope.validation.loader import (  # noqa: E402
    load_selection_header,
    load_selection_json,
)
from reviewscope.validation.models import (  # noqa: E402
    DuplicateLabel,
    ReviewHumanLabel,
    SpecificityLabel,
    TemplatedLabel,
)

DEFAULT_DIR = os.environ.get("RS_VALIDATION_DIR", "validation_data")
DEFAULT_ANNOTATOR = os.environ.get("RS_ANNOTATOR_ID", "")


def _fail(message: str) -> None:
    st.error(message)
    st.stop()


def _fingerprint_from_store(store: DuckDBStore) -> str | None:
    """Read the dataset fingerprint written by the scoring stage, if any."""
    if not store.table_exists("validation_metadata"):
        return None
    row = store.connection().execute(
        "SELECT value FROM validation_metadata WHERE key = 'dataset_fingerprint'"
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _build_label(
    review_id: str,
    entry: dict,
    annotator_id: str,
) -> ReviewHumanLabel:
    """Assemble a human verdict from the widget session state."""
    from reviewscope.validation.models import SampleType

    def rad(keys: tuple[str, str]) -> str | None:
        value = st.session_state.get(f"{review_id}:{keys[1]}")
        return value or None

    templated_raw = rad(("templated", "templated_label"))
    specificity_raw = rad(("specificity", "specificity_label"))
    duplicate_raw = rad(("duplicate", "duplicate_label"))

    label = ReviewHumanLabel(
        review_id=review_id,
        templated_label=(
            TemplatedLabel(templated_raw) if templated_raw in ("organic", "templated", "uncertain") else None
        ),
        template_group_id=st.session_state.get(f"{review_id}:template_group_id") or None,
        specificity_label=(
            SpecificityLabel(specificity_raw)
            if specificity_raw in ("low", "medium", "high", "uncertain")
            else None
        ),
        duplicate_group_id=st.session_state.get(f"{review_id}:duplicate_group_id") or None,
        duplicate_label=(
            DuplicateLabel(duplicate_raw)
            if duplicate_raw in ("unique", "duplicate", "uncertain")
            else None
        ),
        reviewer_notes=st.session_state.get(f"{review_id}:reviewer_notes") or None,
        annotator_id=annotator_id,
        sample_type=SampleType(entry["sample_type"]) if entry.get("sample_type") else None,
        sampling_stratum=entry.get("sampling_stratum"),
    )
    return label


def main() -> None:
    st.title("ReviewScope — Blind Labeling")
    st.caption(
        "Record independent human verdicts. Score-blind by design: detector "
        "outputs are never loaded or shown here."
    )

    with st.sidebar:
        st.header("Configuration")
        data_dir = st.text_input("Validation directory", value=DEFAULT_DIR).strip()
        annotator_id = st.text_input("Annotator id", value=DEFAULT_ANNOTATOR).strip()
        selection_path = st.text_input(
            "Selection file",
            value=str(Path(data_dir) / "sample_selection.json"),
        ).strip()
        dataset_store = st.text_input(
            "Dataset store (.duckdb)",
            value=str(Path(data_dir) / "dataset.duckdb"),
        ).strip()
        annotation_store = st.text_input(
            "Annotation store (.duckdb)",
            value=str(Path(data_dir) / "annotations.duckdb"),
        ).strip()
        st.divider()
        st.caption("Annotator verifies nothing about ReviewScope scores here.")

    if not annotator_id:
        st.info("Enter an annotator id in the sidebar to begin.")
        st.stop()

    selection_file = Path(selection_path)
    dataset_file = Path(dataset_store)

    if not selection_file.exists():
        _fail(f"Selection file not found: {selection_file}")
    if not dataset_file.exists():
        _fail(f"Dataset store not found: {dataset_file}")

    selection = load_selection_json(selection_file)
    selection_header = load_selection_header(selection_file)
    queue = list(selection.keys())
    if not queue:
        _fail(f"Selection file contains no reviews: {selection_file}")

    with DuckDBStore(dataset_file, read_only=True) as store:
        reviews_by_id = {r.review_id: r for r in store.fetch_reviews()}
        dataset_fingerprint = selection_header.get("fingerprint") or _fingerprint_from_store(
            store
        )
    missing = [rid for rid in queue if rid not in reviews_by_id]
    if missing:
        _fail(
            f"{len(missing)} selected review ids are missing from the dataset "
            f"store (e.g. {missing[0]}). Re-run scripts/validation_sample.py."
        )

    with AnnotationStore(annotation_store) as ann:
        if ann.is_finalized():
            meta = ann.batch_metadata() or {}
            st.success("This annotation batch is FINALIZED and read-only.")
            recorded = meta.get("label_count")
            st.markdown(
                f"**Finalized at:** {meta.get('finalized_at') or '—'}  \n"
                f"**Annotator:** {meta.get('annotator_id') or '—'}  \n"
                f"**Labels recorded:** {recorded if recorded is not None else '—'}  \n"
                f"**Dataset fingerprint:** "
                f"`{meta.get('dataset_fingerprint') or 'not recorded'}`"
            )
            st.caption(
                "Scores are never shown in this app. Generate the report with "
                "`scripts/validation_report.py`."
            )
            st.stop()

        labeled = set(ann.labeled_ids())
        skipped = set(st.session_state.get("_skipped", []))
        unlabeled = [rid for rid in queue if rid not in labeled and rid not in skipped]

        st.markdown(
            f"**Progress:** {len(labeled)} / {len(queue)} labeled "
            f"({len(skipped)} skipped this session)."
        )
        st.progress(len(labeled) / max(len(queue), 1), text="annotation progress")
        st.caption(f"Batch status: {ann.batch_status()}")

        if not unlabeled:
            if skipped:
                st.success("All remaining reviews are skipped for this session.")
            else:
                st.success("Annotation batch complete.")
                if dataset_fingerprint:
                    if st.button("Finalize batch (lock labels)", type="primary"):
                        ann.finalize(
                            annotator_id=annotator_id,
                            dataset_fingerprint=dataset_fingerprint,
                            label_count=len(labeled),
                        )
                        st.rerun()
                else:
                    st.info(
                        "No dataset fingerprint found; finalize from the CLI with "
                        "`scripts/validation_finalize.py` once the dataset is scored."
                    )
            st.stop()

        current_id = unlabeled[0]
        review = reviews_by_id[current_id]
        entry = selection[current_id]

        col_main, col_facts = st.columns([3, 1], gap="large")

        with col_main:
            st.subheader(f"Review `{review.review_id}`")
            st.markdown(f"### {review.text_or_empty() or '(no text)'}")
            st.divider()

            st.markdown("**1. Is this review templated / synthetic-like?**")
            templated = st.radio(
                "Template-family judgment",
                options=[TemplatedLabel.ORGANIC.value, TemplatedLabel.TEMPLATED.value, TemplatedLabel.UNCERTAIN.value],
                format_func=lambda v: v.title(),
                index=0,
                key=f"{current_id}:templated_label",
            )
            st.text_input(
                "Template group id (optional, e.g. when several reviews reuse the "
                "same template family)",
                key=f"{current_id}:template_group_id",
            )

            st.markdown("**2. How specific/informative is this review?**")
            st.radio(
                "Specificity judgment",
                options=[SpecificityLabel.LOW.value, SpecificityLabel.MEDIUM.value, SpecificityLabel.HIGH.value, SpecificityLabel.UNCERTAIN.value],
                format_func=lambda v: v.title(),
                index=2,
                key=f"{current_id}:specificity_label",
            )

            st.markdown("**3. Is this review a duplicate of other reviews in the batch?**")
            duplicate = st.radio(
                "Duplicate judgment",
                options=[DuplicateLabel.UNIQUE.value, DuplicateLabel.DUPLICATE.value, DuplicateLabel.UNCERTAIN.value],
                format_func=lambda v: v.title(),
                index=0,
                key=f"{current_id}:duplicate_label",
            )
            st.text_input(
                "Duplicate group id (required when 'duplicate' is chosen; reuse "
                "the same id for members of one group)",
                key=f"{current_id}:duplicate_group_id",
            )

            st.markdown("**Notes**")
            st.text_area("Freeform notes", key=f"{current_id}:reviewer_notes")

            if duplicate == DuplicateLabel.DUPLICATE.value and not st.session_state.get(
                f"{current_id}:duplicate_group_id"
            ):
                st.warning("'duplicate' selected without a duplicate group id.")
            if templated == TemplatedLabel.TEMPLATED.value and not st.session_state.get(
                f"{current_id}:template_group_id"
            ):
                st.info("'templated' selected without a template group id (allowed).")

            save = st.button("Save & Next", type="primary", width="stretch")
            skip = st.button("Skip for now", width="stretch")

            if save:
                label = _build_label(current_id, entry, annotator_id)
                ann.save_label(label)
                st.session_state.setdefault("_skipped", set()).discard(current_id)
                st.rerun()
            if skip:
                st.session_state.setdefault("_skipped", set()).add(current_id)
                st.rerun()

        with col_facts:
            st.markdown("**Context**")
            facts: list[tuple[str, str]] = [
                ("Place", review.place_name or review.place_id),
                ("Category", review.place_category or "—"),
                ("Rating", f"{'★' * (review.rating or 0)}{'☆' * (5 - (review.rating or 0))}"),
                ("Published", review.published_at or "—"),
                ("Reviewer", review.reviewer_name or review.reviewer_id),
                ("City", review.city or "—"),
            ]
            for key, value in facts:
                st.markdown(f"**{key}**  \n{value}")


if __name__ == "__main__":
    main()
