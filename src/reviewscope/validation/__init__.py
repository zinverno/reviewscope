"""Phase 15 — Real-data validation measurement machinery.

This package is the *measurement* layer of ReviewScope: it carries human
ground-truth labels, folds them against the outputs the **production**
pipeline already produces, and reports the result. It must never be used to
tune production detectors, thresholds or weights (see
``docs/REAL_DATA_VALIDATION.md`` for the calibration-isolation rules).

Public entry points:

* :mod:`reviewscope.validation.models` — human-label data model + CSV schema
* :mod:`reviewscope.validation.loader` — tolerant label/dataset I/O and the
  label-template tooling
* :mod:`reviewscope.validation.sampling` — deterministic evaluation SRS and
  disjoint score/duplicate-enriched challenge sampling
* :mod:`reviewscope.validation.scoring` — production-detector orchestration
  with dataset-fingerprint store hygiene
* :mod:`reviewscope.validation.metrics` — human-vs-ReviewScope metric folds
* :mod:`reviewscope.validation.report` — Markdown + JSON report assembly
* :mod:`reviewscope.validation.annotation` — persisted, resumable annotation
  backend used by the blind labeling app
"""

from reviewscope.validation.models import ANNOTATION_SCHEMA_VERSION

__all__ = ["ANNOTATION_SCHEMA_VERSION"]
