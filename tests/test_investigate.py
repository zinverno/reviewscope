"""Phase 18 — case investigation workspace (SPEC.md §29, §36).

The workspace lets a reader open one repeated-text family from the Duplicates
list or from Discover and stay with it. These tests pin the contract:

* family identity is member-based (place id + sorted review ids), deterministic
  and independent of run-local group ids;
* the workspace shows members and *direct* links only, marks absent pairs
  explicitly, and never claims all-pair similarity;
* the selection survives reruns and page navigation and is invalidated when
  the dataset changes;
* Discover opens a place's largest family into the workspace;
* missing edge data renders as unavailable, never as an observed zero;
* the copy stays neutral — no verdict wording anywhere.

Detector behaviour itself is covered by ``tests/test_duplicates.py`` and is
not modified here. Browser is unavailable in this environment, so the page
flows run through ``AppTest`` (SPEC.md §39).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from reviewscope.analysis.duplicates import DuplicateDetector, DuplicateGroup
from reviewscope.config import CONFIG
from reviewscope.models.review import NormalizedReview
from reviewscope.storage import DuckDBStore
from reviewscope.ui import common
from reviewscope.ui.discover import _largest_family_size
from reviewscope.ui.duplicates import EVIDENCE_NOTE, FAMILY_DEFINITION, TRANSITIVE_LABEL
from reviewscope.ui.investigate import (
    BACK_KEY,
    CLEAR_KEY,
    FAMILY_KEY,
    MEMBER_KEY,
    RESUME_KEY,
    SWITCHER_KEY,
    TEXT_KEY,
    WORKSPACE_INTRO,
    family_identity,
    family_options,
    kind_phrase,
    member_frame,
    option_label,
    relationship_frame,
)

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

#: Verified against the real detectors: an exact family of 4, a second family
#: of 2, and one review that stays on its own (plus a second, clean place).
EXACT_TEXT = "Отличное место, превосходный кофе и внимательный персонал каждый раз."
NEAR_A = "Быстрое обслуживание и вкусные десерты по приятным ценам."
NEAR_B = "Быстрое обслуживание и вкусные десерты по приятным ценам!"
UNIQUE_DUP = "Совсем другое место совсем других слов тут нет."
UNIQUE_CLEAN_1 = "Обычная парикмахерская без каких-либо заметных особенностей."
UNIQUE_CLEAN_2 = "Тихо, аккуратно, мастера вежливые — всё понравилось нам."

#: Members of the chain family, in ingestion order (labels A, B, C).
CHAIN_PLACE = "chain"
_CHAIN_TEXTS = [
    "Great coffee and the staff were genuinely friendly that morning.",
    "Excellent espresso with warm service and a quiet corner seat.",
    "Lovely pastries, calm atmosphere and easy parking out front.",
]

_MEMBER_COLUMNS = ["Member", "Review", "Reviewer", "Rating", "Published", "Direct links", "Link kinds"]

#: Verdict wording that must never appear (the mandatory disclaimer itself is
#: asserted separately).
_FORBIDDEN = ("fake", "manipulat", "scam", "suspicious", "cheat", "ai-written")

#: Positive wordings that would read as an all-pair similarity claim.
_ALL_PAIR_CLAIMS = (
    "near-copies of each other",
    "similarity across the family",
    "every pair of reviews",
    "all of the reviews are",
    "mutually similar",
)


def _review(
    review_id: str,
    place_id: str,
    text: str,
    *,
    rating: int = 5,
    day: int = 1,
    place_name: str = "Fixture Place",
) -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place_id,
        place_name=place_name,
        place_category="cafe",
        reviewer_id=f"user-{review_id}",
        rating=rating,
        text=text,
        published_at=f"2026-09-{day:02d}T10:00:00",
    )


def _family_reviews() -> list[NormalizedReview]:
    reviews = [
        _review("dup-0", "dupcafe", EXACT_TEXT, rating=5, day=1, place_name="Dup Cafe"),
        _review("dup-1", "dupcafe", EXACT_TEXT, rating=5, day=1, place_name="Dup Cafe"),
        _review("dup-2", "dupcafe", EXACT_TEXT, rating=4, day=2, place_name="Dup Cafe"),
        _review("dup-3", "dupcafe", EXACT_TEXT, rating=4, day=2, place_name="Dup Cafe"),
        _review("dup-4", "dupcafe", NEAR_A, rating=3, day=3, place_name="Dup Cafe"),
        _review("dup-5", "dupcafe", NEAR_B, rating=4, day=3, place_name="Dup Cafe"),
        _review("dup-6", "dupcafe", UNIQUE_DUP, rating=4, day=4, place_name="Dup Cafe"),
        _review("clean-0", "cleancafe", UNIQUE_CLEAN_1, rating=5, day=1, place_name="Clean Cafe"),
        _review("clean-1", "cleancafe", UNIQUE_CLEAN_2, rating=3, day=2, place_name="Clean Cafe"),
    ]
    return reviews


def _chain_embeddings(n: int, gap_deg: float, dim: int = 384) -> np.ndarray:
    """Unit vectors on a plane: neighbours are similar, non-neighbours are not."""
    mid = (n - 1) / 2
    out = np.zeros((n, dim), dtype=np.float32)
    for i in range(n):
        theta = math.radians((i - mid) * gap_deg)
        out[i, 0] = math.cos(theta)
        out[i, 1] = math.sin(theta)
    return out


def _build_db(path: Path, reviews: list[NormalizedReview], embeddings=None) -> str:
    model = CONFIG.embedding.model_name
    with DuckDBStore(db_path=path, read_only=False) as store:
        store.ingest(reviews)
        if embeddings is not None:
            store.store_cached_embeddings(
                [
                    (r.review_id, r.fingerprint(), model, embeddings[i].tolist())
                    for i, r in enumerate(reviews)
                ]
            )
    return str(path)


@pytest.fixture(scope="module")
def family_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("familydb") / "family.duckdb"
    return _build_db(path, _family_reviews())


@pytest.fixture(scope="module")
def family_db_copy(tmp_path_factory) -> str:
    """A different file with the same places and the same family identities."""
    path = tmp_path_factory.mktemp("familydb2") / "family_copy.duckdb"
    return _build_db(path, _family_reviews())


@pytest.fixture(scope="module")
def chain_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("chaindb") / "chain.duckdb"
    reviews = [
        _review(f"{CHAIN_PLACE}-{i}", CHAIN_PLACE, _CHAIN_TEXTS[i], day=i + 1) for i in range(3)
    ]
    return _build_db(path, reviews, _chain_embeddings(len(reviews), 20.0))


def _group(ids, edges=(), *, group_id: int = 1, avg_similarity: float = 0.9) -> DuplicateGroup:
    return DuplicateGroup(
        group_id=group_id,
        review_ids=list(ids),
        edges=list(edges),
        avg_similarity=avg_similarity,
    )


# ---------------------------------------------------------------------------
# Pure helpers — identity, options, labels
# ---------------------------------------------------------------------------


class TestFamilyIdentity:
    def test_identity_is_the_sorted_member_set(self) -> None:
        assert family_identity("p", ["r2", "r1", "r0"]) == family_identity("p", ["r0", "r1", "r2"])

    def test_identity_is_place_scoped(self) -> None:
        assert family_identity("p1", ["r0", "r1"]) != family_identity("p2", ["r0", "r1"])

    def test_identity_ignores_group_id_and_member_order(self) -> None:
        first = _group(["a", "b", "c"], group_id=7)
        rebuilt = _group(["c", "a", "b"], group_id=3)
        assert family_identity("p", first.review_ids) == family_identity("p", rebuilt.review_ids)

    def test_different_member_sets_differ(self) -> None:
        assert family_identity("p", ["a", "b"]) != family_identity("p", ["a", "c"])


class TestFamilyOptions:
    def _groups(self) -> list[DuplicateGroup]:
        return [
            _group(["a", "b"], group_id=1, avg_similarity=0.95),
            _group(["c", "d", "e", "f"], group_id=2, avg_similarity=0.87),
            _group(["g", "h", "i"], group_id=3, avg_similarity=0.91),
        ]

    def test_largest_family_comes_first(self) -> None:
        options = family_options(self._groups(), "p")
        assert [len(g.review_ids) for _k, g in options] == [4, 3, 2]

    def test_order_is_deterministic_under_input_order(self) -> None:
        groups = self._groups()
        forward = [k for k, _g in family_options(groups, "p")]
        backward = [k for k, _g in family_options(list(reversed(groups)), "p")]
        assert forward == backward

    def test_ties_break_on_identity(self) -> None:
        tied = [
            _group(["m2", "m1"], group_id=1, avg_similarity=0.9),
            _group(["m4", "m3"], group_id=2, avg_similarity=0.9),
        ]
        keys = [k for k, _g in family_options(tied, "p")]
        assert keys == sorted(keys)

    def test_singletons_are_excluded(self) -> None:
        options = family_options([_group(["solo"])], "p")
        assert options == []


class TestOptionLabel:
    def test_states_size_span_and_link_facts(self) -> None:
        group = _group(
            ["a", "b", "c"],
            edges=[("a", "b", "exact", 1.0), ("b", "c", "exact", 1.0)],
            avg_similarity=0.97,
        )
        label = option_label(group)
        assert "3 reviews (A–C)" in label
        assert "2 direct links" in label
        assert "similarity 0.97" in label

    def test_edgeless_group_says_structure_not_stored(self) -> None:
        label = option_label(_group(["a", "b"]))
        assert "pair-level structure not stored" in label
        assert "direct links" not in label

    def test_never_contains_verdict_words(self) -> None:
        label = option_label(_group(["a", "b"], edges=[("a", "b", "exact", 1.0)]))
        for word in _FORBIDDEN:
            assert word not in label.lower()


class TestKindPhrase:
    def test_exact_link_is_identical_text(self) -> None:
        assert kind_phrase("exact", 1.0) == "identical text"

    def test_semantic_link_carries_similarity(self) -> None:
        assert kind_phrase("semantic", 0.93) == "semantic similarity 0.93"

    def test_lexical_links_carry_kind_and_score(self) -> None:
        assert kind_phrase("near", 0.91).startswith("near duplicate · similarity 0.91")
        assert kind_phrase("fuzzy", 0.86).startswith("fuzzy match · similarity 0.86")


# ---------------------------------------------------------------------------
# Pure helpers — derived tables
# ---------------------------------------------------------------------------


def _by_id(reviews: list[NormalizedReview]) -> dict:
    return {r.review_id: r for r in reviews}


class TestMemberFrame:
    def test_direct_link_counts_per_member(self) -> None:
        reviews = [_review(f"r{i}", "p", f"text {i}") for i in range(3)]
        group = _group(
            ["r0", "r1", "r2"],
            edges=[("r0", "r1", "exact", 1.0), ("r1", "r2", "semantic", 0.91)],
        )
        frame = member_frame(group, _by_id(reviews))
        assert list(frame.columns) == _MEMBER_COLUMNS
        assert frame["Direct links"].tolist() == ["1 of 2", "2 of 2", "1 of 2"]
        assert "semantic" in frame.loc[frame["Member"] == "B", "Link kinds"].item()

    def test_missing_edge_data_reads_not_stored(self) -> None:
        reviews = [_review(f"r{i}", "p", f"text {i}") for i in range(2)]
        frame = member_frame(_group(["r0", "r1"]), _by_id(reviews))
        assert frame["Direct links"].tolist() == ["not stored", "not stored"]
        assert frame["Link kinds"].tolist() == ["not stored", "not stored"]

    def test_unknown_member_record_renders_placeholders(self) -> None:
        frame = member_frame(_group(["ghost"]), {})
        assert frame["Reviewer"].item() == "—"
        assert frame["Rating"].item() == "—"
        assert frame["Published"].item() == "—"


class TestRelationshipFrame:
    def _chain(self) -> DuplicateGroup:
        return _group(
            ["r0", "r1", "r2"],
            edges=[
                ("r0", "r1", "exact", 1.0),
                ("r1", "r2", "semantic", 0.91),
            ],
        )

    def test_absent_pair_is_marked_not_linked(self) -> None:
        frame = relationship_frame(self._chain())
        assert frame is not None
        assert frame.loc["A", "C"] == "—"
        assert frame.loc["C", "A"] == "—"

    def test_edges_render_symmetrically(self) -> None:
        frame = relationship_frame(self._chain())
        assert frame.loc["A", "B"] == "identical text"
        assert frame.loc["B", "A"] == "identical text"
        assert frame.loc["B", "C"] == "semantic similarity 0.91"

    def test_diagonal_is_blank(self) -> None:
        frame = relationship_frame(self._chain())
        assert [frame.loc[label, label] for label in ["A", "B", "C"]] == ["", "", ""]

    def test_complete_family_has_no_dash_cells(self) -> None:
        complete = _group(
            ["r0", "r1", "r2"],
            edges=[("r0", "r1", "exact", 1.0), ("r0", "r2", "exact", 1.0), ("r1", "r2", "exact", 1.0)],
        )
        frame = relationship_frame(complete)
        assert frame is not None
        cells = [frame.loc[a, b] for a in "ABC" for b in "ABC" if a != b]
        assert set(cells) == {"identical text"}

    def test_edgeless_group_returns_none(self) -> None:
        assert relationship_frame(_group(["r0", "r1"])) is None


class TestLargestFamilyGate:
    def test_reads_the_frame_value(self) -> None:
        assert _largest_family_size(pd.Series({"largest_duplicate_group": 4})) == 4

    def test_missing_value_is_zero(self) -> None:
        assert _largest_family_size(pd.Series(dtype=object)) == 0

    def test_nan_and_none_are_zero(self) -> None:
        assert _largest_family_size(pd.Series({"largest_duplicate_group": float("nan")})) == 0
        assert _largest_family_size(pd.Series({"largest_duplicate_group": None})) == 0


# ---------------------------------------------------------------------------
# AppTest — page flows
# ---------------------------------------------------------------------------


def _app(db_path: str, monkeypatch) -> AppTest:
    monkeypatch.setattr(common, "DEFAULT_DB_PATH", db_path)
    at = AppTest.from_file(APP_PATH, default_timeout=300)
    at.run()
    assert not list(at.exception), [e.value for e in at.exception]
    return at


def _run(at: AppTest) -> None:
    at.run()
    assert not list(at.exception), [e.value for e in at.exception]


def _navigate(at: AppTest, page: str, place: str | None = None) -> None:
    if place is not None:
        at.sidebar.selectbox[0].set_value(place)
        _run(at)
    at.sidebar.radio[0].set_value(page)
    _run(at)


def _click(at: AppTest, key: str) -> None:
    at.button(key=key).click()
    _run(at)


def _rendered(at: AppTest) -> str:
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption]
    parts += [h.value for h in at.header]
    parts += [s.value for s in at.subheader]
    parts += [i.value for i in at.info]
    parts += [m.label for m in at.metric]
    return "\n".join(parts)


def _subheaders(at: AppTest) -> list[str]:
    return [s.value for s in at.subheader]


def _frame(at: AppTest, columns: list[str]) -> pd.DataFrame:
    for element in at.dataframe:
        if list(element.value.columns) == columns:
            return element.value
    raise AssertionError(f"no dataframe with columns {columns}")


def _workspace_open(at: AppTest) -> bool:
    return "Case investigation" in _subheaders(at)


class TestWorkspaceFromDuplicatesList:
    def test_every_family_card_offers_the_workspace(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        labels = [b.label for b in at.button if b.label == "Open investigation workspace"]
        assert len(labels) == 2

    def test_open_renders_the_workspace(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        assert _workspace_open(at)
        assert at.selectbox(key=SWITCHER_KEY)
        assert at.selectbox(key=MEMBER_KEY)
        assert at.text_area(key=TEXT_KEY)
        assert WORKSPACE_INTRO.split(".")[0] in _rendered(at)
        assert FAMILY_DEFINITION in _rendered(at)

    def test_member_table_lists_the_exact_family(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        frame = _frame(at, _MEMBER_COLUMNS)
        assert len(frame) == 4
        assert frame["Direct links"].tolist() == ["3 of 3"] * 4
        assert (frame["Link kinds"] == "identical text").all()
        assert frame["Review"].str.startswith("dup-").all()

    def test_relationship_matrix_is_complete_for_the_exact_family(
        self, family_db, monkeypatch
    ) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        frame = _frame(at, ["A", "B", "C", "D"])
        cells = [frame.loc[a, b] for a in "ABCD" for b in "ABCD" if a != b]
        assert set(cells) == {"identical text"}
        assert EVIDENCE_NOTE in _rendered(at)

    def test_back_returns_to_the_list_but_keeps_the_selection(
        self, family_db, monkeypatch
    ) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        _click(at, BACK_KEY)
        assert not _workspace_open(at)
        metrics = {m.label for m in at.metric}
        assert "Repeated-text families" in metrics
        resume_infos = [i.value for i in at.info if "Investigation in progress" in i.value]
        assert resume_infos
        _click(at, RESUME_KEY)
        assert _workspace_open(at)

    def test_clear_selection_removes_the_resume_notice(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        _click(at, BACK_KEY)
        _click(at, CLEAR_KEY)
        assert not _workspace_open(at)
        assert not [i for i in at.info if "Investigation in progress" in i.value]
        assert at.session_state.get(FAMILY_KEY) is None

    def test_selection_survives_page_navigation(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        for page in ("Overview", "Topics", "Duplicates"):
            _navigate(at, page)
            if page == "Duplicates":
                assert _workspace_open(at)
            else:
                assert not _workspace_open(at)

    def test_switching_place_falls_back_to_the_list(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        _navigate(at, "Duplicates", place="cleancafe")
        assert not _workspace_open(at)
        infos = [i.value for i in at.info]
        assert any("No repeated-text families" in value for value in infos)


class TestWorkspaceInspectorAndSwitcher:
    def test_inspector_shows_only_the_selected_members_links(
        self, family_db, monkeypatch
    ) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        at.selectbox(key=MEMBER_KEY).set_value("dup-0")
        _run(at)
        assert at.text_area(key=TEXT_KEY).value == EXACT_TEXT
        rendered = _rendered(at)
        assert "**3 direct links** of 3 possible:" in rendered
        assert EVIDENCE_NOTE in rendered
        for claim in _ALL_PAIR_CLAIMS:
            assert claim not in rendered

    def test_switcher_moves_between_families_in_place(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        assert len(_frame(at, _MEMBER_COLUMNS)) == 4
        second_key = family_identity("dupcafe", ["dup-4", "dup-5"])
        at.selectbox(key=SWITCHER_KEY).set_value(second_key)
        _run(at)
        assert _workspace_open(at)
        frame = _frame(at, _MEMBER_COLUMNS)
        assert sorted(frame["Review"].tolist()) == ["dup-4", "dup-5"]
        assert at.session_state[FAMILY_KEY] == second_key

    def test_inspector_of_the_second_family_reads_its_own_text(
        self, family_db, monkeypatch
    ) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_1")
        at.selectbox(key=MEMBER_KEY).set_value("dup-5")
        _run(at)
        assert at.text_area(key=TEXT_KEY).value == NEAR_B
        rendered = _rendered(at)
        assert "**1 direct link** of 1 possible:" in rendered
        assert "- → **A** · " in rendered

    def test_second_family_matrix_marks_its_single_pair(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_1")
        frame = _frame(at, ["A", "B"])
        cell = frame.loc["A", "B"]
        assert cell.startswith(("near duplicate", "fuzzy match", "semantic similarity"))


class TestTransitiveFamilyWorkspace:
    def test_chain_family_shows_the_missing_pair_as_unlinked(self, chain_db, monkeypatch) -> None:
        at = _app(chain_db, monkeypatch)
        _navigate(at, "Duplicates")
        _click(at, "open_family_ws_0")
        rendered = _rendered(at)
        assert TRANSITIVE_LABEL in rendered
        assert EVIDENCE_NOTE in rendered
        frame = _frame(at, _MEMBER_COLUMNS)
        assert frame["Direct links"].tolist() == ["1 of 2", "2 of 2", "1 of 2"]
        matrix = _frame(at, ["A", "B", "C"])
        assert matrix.loc["A", "C"] == "—"
        assert matrix.loc["C", "A"] == "—"
        assert matrix.loc["A", "B"].startswith("semantic similarity")

    def test_workspace_copy_never_claims_all_pair_similarity(self, chain_db, monkeypatch) -> None:
        at = _app(chain_db, monkeypatch)
        _navigate(at, "Duplicates")
        _click(at, "open_family_ws_0")
        rendered = _rendered(at)
        for claim in _ALL_PAIR_CLAIMS:
            assert claim not in rendered, claim
        assert FAMILY_DEFINITION in rendered


class TestEdgelessFamilyWorkspace:
    def test_missing_edge_data_renders_as_unavailable(self, family_db, monkeypatch) -> None:
        original = DuplicateDetector.detect

        def _without_edges(self, reviews, embeddings=None):
            groups = original(self, reviews, embeddings)
            for group in groups:
                group.edges = []
            return groups

        monkeypatch.setattr(DuplicateDetector, "detect", _without_edges)
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        infos = [i.value for i in at.info]
        assert any("Pair-level structure not stored" in value for value in infos)
        assert not [d for d in at.dataframe if list(d.value.columns) == ["A", "B", "C", "D"]]
        frame = _frame(at, _MEMBER_COLUMNS)
        assert set(frame["Direct links"]) == {"not stored"}
        rendered = _rendered(at)
        assert "pair-level structure not stored" in rendered


class TestDiscoverEntry:
    def test_investigate_family_opens_the_largest_family(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Discover")
        at.selectbox(key="discover_focus_place").set_value("dupcafe")
        _run(at)
        _click(at, "discover_investigate_family")
        assert at.sidebar.radio[0].value == "Duplicates"
        assert at.sidebar.selectbox[0].value == "dupcafe"
        assert _workspace_open(at)
        frame = _frame(at, _MEMBER_COLUMNS)
        assert len(frame) == 4

    def test_place_without_families_is_gated(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Discover")
        at.selectbox(key="discover_focus_place").set_value("cleancafe")
        _run(at)
        button = at.button(key="discover_investigate_family")
        try:
            button.click()
        except Exception:
            pass
        _run(at)
        assert at.sidebar.radio[0].value == "Discover"


class TestDatasetInvalidation:
    def test_switching_the_dataset_clears_the_selection(
        self, family_db, family_db_copy, monkeypatch
    ) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        _click(at, "open_family_ws_0")
        assert _workspace_open(at)
        at.sidebar.text_input[0].set_value(family_db_copy)
        _run(at)
        assert at.sidebar.selectbox[0].value == "dupcafe"
        assert not _workspace_open(at)
        assert not [i for i in at.info if "Investigation in progress" in i.value]
        _navigate(at, "Duplicates")
        assert not _workspace_open(at)


class TestNeutralCopy:
    def test_workspace_contains_no_verdict_wording(self, chain_db, monkeypatch) -> None:
        at = _app(chain_db, monkeypatch)
        _navigate(at, "Duplicates")
        _click(at, "open_family_ws_0")
        rendered = _rendered(at).lower()
        stripped = rendered.replace("does not assert fraud", "")
        for word in _FORBIDDEN:
            assert word not in stripped, word
        assert "does not assert fraud" in rendered

    def test_workspace_labels_the_evidence_as_detector_output(
        self, chain_db, monkeypatch
    ) -> None:
        at = _app(chain_db, monkeypatch)
        _navigate(at, "Duplicates")
        _click(at, "open_family_ws_0")
        rendered = _rendered(at)
        assert "direct link" in rendered
        assert "intermediate" in rendered
