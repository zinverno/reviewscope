"""Phase 15 — regression tests for the sampling entry path.

``scripts/validation_sample.py`` consumes the production ingestion contract.
``load_reviews`` returns an ``ingestion.base.LoadResult`` whose diagnostics live
on ``result.report`` (a ``ValidationReport``); there is no ``result.warnings``.
These tests guard that contract so the blind-sampling pipeline cannot silently
re-crash on the real corpus.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from reviewscope.ingestion.base import LoadResult

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_MODULE_NAME = "validation_sample"


def _load_script():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPTS / f"{_MODULE_NAME}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


sample_mod = _load_script()
_ = pytest


def _tiny_csv(path: Path) -> Path:
    duplicate_text = "Очень понравилось, всё на высшем уровне, обязательно придём ещё раз!"
    rows = (
        [
            {
                "review_id": f"p1-r{i}",
                "place_id": "p1",
                "place_name": "ТЦ Галерея",
                "place_category": "Торговый центр",
                "reviewer_id": f"user-{i}",
                "rating": 5,
                "text": duplicate_text,
            }
            for i in range(4)
        ]
        + [
            {
                "review_id": f"p2-r{i}",
                "place_id": "p2",
                "place_name": "Кафе Утро",
                "place_category": "Кафе",
                "reviewer_id": f"barista-{i}",
                "rating": 4,
                "text": f"В этот раз попробовали новый десерт с кремом {i}",
            }
            for i in range(3)
        ]
        + [
            {  # malformed row: no review_id -> skipped with a warning
                "review_id": "",
                "place_id": "p3",
                "rating": 5,
                "text": "пусто",
            }
        ]
    )
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_load_contract_diagnostics_live_on_report():
    result = LoadResult()
    assert not hasattr(result, "warnings")
    assert result.report.total_rows == 0
    assert result.report.warnings == []
    assert result.report.warning_count == 0


def test_sampling_entry_path_passes_loading(tmp_path, capsys):
    dataset = _tiny_csv(tmp_path / "tiny.csv")
    out = tmp_path / "out"
    code = sample_mod.main(
        [
            str(dataset),
            "--out-dir",
            str(out),
            "--evaluation-n",
            "3",
            "--challenge-high",
            "1",
            "--challenge-medium",
            "1",
            "--challenge-low",
            "1",
            "--challenge-duplicate",
            "1",
            "--seed",
            "20260901",
            "--no-embeddings",
        ]
    )
    assert code == 0

    diagnostics = capsys.readouterr().out
    assert "imported: 8" in diagnostics
    assert "valid: 7" in diagnostics
    assert "skipped: 1" in diagnostics
    assert "warnings: 1" in diagnostics
    assert "[warning]" in diagnostics

    scores = json.loads((out / "score_table.json").read_text(encoding="utf-8"))
    assert scores["schema"] == "reviewscope_scores_1"
    assert len(scores["reviews"]) == 7

    selection = json.loads((out / "sample_selection.json").read_text(encoding="utf-8"))
    assert selection["seed"] == 20260901
    assert selection["evaluation_count"] == 3
    assert 0 <= sum(selection["challenge_counts"].values()) < 4  # small pools cannot over-fill
    assert len(selection["entries"]) == (
        selection["evaluation_count"] + sum(selection["challenge_counts"].values())
    )
    selected_ids = {entry["review_id"] for entry in selection["entries"]}
    scored_ids = {row["review_id"] for row in scores["reviews"]}
    assert selected_ids <= scored_ids
    assert {
        entry["review_id"] for entry in selection["entries"] if entry["sample_type"] == "evaluation"
    }.issubset(scored_ids)

    template = pd.read_csv(out / "label_template.csv", dtype=str)
    assert set(template["review_id"]) == selected_ids
    assert template["templated_label"].isna().all()
