"""Phase 17G — connected-family presentation (SPEC.md §29).

A ``DuplicateGroup`` is a *connected component* of detected pair relationships:
A–B and B–C put A, B and C in one family even when A–C never passed a
threshold. These tests pin the presentation contract around that fact:

* the production group stays one connected family (membership unchanged);
* link / possible-pair counts are accurate;
* a transitive family says so, in neutral wording;
* the copy never claims all-pair similarity;
* rating alignment is shown as context only and never drives membership.

Detector behaviour itself is covered by ``tests/test_duplicates.py`` and is not
modified here.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from reviewscope.analysis.duplicates import DuplicateDetector
from reviewscope.config import CONFIG
from reviewscope.models.review import NormalizedReview
from reviewscope.storage.duckdb_store import DuckDBStore
from reviewscope.ui.duplicates import (
    EVIDENCE_NOTE,
    FAMILY_DEFINITION,
    TRANSITIVE_DETAIL,
    TRANSITIVE_LABEL,
    FamilyStructure,
    interpretation,
    member_evidence,
    member_labels,
    rating_context,
    rating_line,
)

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from reviewscope.ui import common  # noqa: E402

#: Cosine between adjacent links of a chain: above the 0.88 threshold, while
#: links one step further apart sit well below it.
_GAP_DEG = 20.0

#: Texts chosen to be mutually dissimilar so the *lexical* detectors stay quiet
#: and only the supplied embeddings create links.
_CHAIN_TEXTS = [
    "Great coffee and the staff were genuinely friendly that morning.",
    "Excellent espresso with warm service and a quiet corner seat.",
    "Lovely pastries, calm atmosphere and easy parking out front.",
    "The hiking trail was muddy but the views at the top were worth it.",
    "Service was slow on Sunday yet the Sunday brunch made up for it.",
]

#: A fuzzy/near pair (0–1) plus an unrelated third review, so one family mixes
#: lexical and semantic links.
_MIXED_TEXTS = [
    "Хорошая стоматология, лечение было без боли, врач всё рассказал по снимкам. Цена нормальная.",
    "Хорошая стоматология, лечение прошло без боли, врач рассказал всё по снимкам. Цена нормальная.",
    "Совершенно непохожий текст про другое место в другом городе и другое время.",
]

#: Gap that keeps the 0–1 semantic cosine (0.90) *below* the near-duplicate
#: RapidFuzz score (0.925), so the lexical classification wins for that pair.
_MIXED_GAP_DEG = math.degrees(math.acos(0.90))

_EXACT_TEXT = "Одинаковый текст отзыва про уютное кафе с десертами."

#: Positive wordings that would read as an all-pair similarity claim. The
#: required disclaimer legitimately contains "Not every pair ... must directly
#: pass", and the detection breakdown legitimately labels an "identical text"
#: link kind, so neither is listed here; the disclaimer is asserted separately.
_ALL_PAIR_CLAIMS = (
    "near-copies of each other",
    "similarity across the family",
    "every pair of reviews",
    "all of the reviews are",
    "mutually similar",
)


def _review(i: int, text: str, rating: int = 4, place: str = "chain") -> NormalizedReview:
    return NormalizedReview(
        review_id=f"{place}-{i}",
        place_id=place,
        reviewer_id=f"user-{i}",
        rating=rating,
        text=text,
        published_at=f"2026-08-{1 + i:02d}T10:00:00",
    )


def _chain_embeddings(n: int, gap_deg: float, dim: int = 384) -> np.ndarray:
    """Unit vectors on a plane: neighbours are similar, non-neighbours are not."""
    mid = (n - 1) / 2
    out = np.zeros((n, dim), dtype=np.float32)
    for i in range(n):
        theta = math.radians((i - mid) * gap_deg)
        out[i, 0] = math.cos(theta)
        out[i, 1] = math.sin(theta)
    return out


def _detect(reviews, embeddings):
    groups = DuplicateDetector().detect(reviews, embeddings=embeddings)
    assert len(groups) == 1, f"expected one family, got {len(groups)}"
    return groups[0]


@pytest.fixture(scope="module")
def mixed_family():
    """A family whose links mix lexical (near-duplicate) and semantic detection."""
    reviews = [_review(i, _MIXED_TEXTS[i], place="mix") for i in range(3)]
    return _detect(reviews, _chain_embeddings(3, _MIXED_GAP_DEG))


def _chain(n: int):
    reviews = [_review(i, _CHAIN_TEXTS[i]) for i in range(n)]
    return reviews, _detect(reviews, _chain_embeddings(n, _GAP_DEG))


# ---------------------------------------------------------------------------
# Synthetic chain: A–B, B–C, but not A–C
# ---------------------------------------------------------------------------


class TestTransitiveChain:
    def test_chain_forms_one_connected_family(self) -> None:
        reviews, group = _chain(3)
        assert sorted(group.review_ids) == sorted(r.review_id for r in reviews)

    def test_pair_and_link_counts_are_accurate(self) -> None:
        _, group = _chain(3)
        st = FamilyStructure(group)
        assert st.n == 3
        assert st.possible == 3
        assert st.links == 2  # A–B and B–C only; A–C was never detected
        assert st.density == pytest.approx(2 / 3)
        assert st.by_kind == {"exact": 0, "fuzzy": 0, "near": 0, "semantic": 2}
        assert len(group.edges) == st.links
        # avg_similarity is the mean over detected links, not over all pairs
        assert group.avg_similarity == pytest.approx(
            sum(e[3] for e in group.edges) / len(group.edges), abs=1e-4
        )

    def test_missing_pair_is_absent_from_the_edge_list(self) -> None:
        reviews, group = _chain(3)
        a, b, c = (r.review_id for r in reviews)
        pairs = {(e[0], e[1]) for e in group.edges} | {(e[1], e[0]) for e in group.edges}
        assert (a, b) in pairs and (b, c) in pairs
        assert (a, c) not in pairs and (c, a) not in pairs

    def test_transitive_indication_is_set(self) -> None:
        _, group = _chain(3)
        st = FamilyStructure(group)
        assert st.transitive is True
        assert st.sparse is False  # 2 of 3 links is still a majority

    def test_transitive_detail_shows_when_fewer_than_half_of_pairs_link(
        self,
    ) -> None:
        _, group = _chain(5)
        st = FamilyStructure(group)
        assert st.n == 5
        assert st.possible == 10
        assert st.links == 4
        assert st.transitive is True
        assert st.sparse is True
        assert st.density == pytest.approx(0.4)

    def test_direct_pair_is_never_marked_transitive(self) -> None:
        reviews = [_review(0, _CHAIN_TEXTS[0]), _review(1, _CHAIN_TEXTS[1])]
        group = _detect(reviews, _chain_embeddings(2, _GAP_DEG))
        st = FamilyStructure(group)
        assert st.n == 2
        assert st.possible == 1
        assert st.links == 1
        assert st.density == pytest.approx(1.0)
        assert st.transitive is False
        assert st.sparse is False


class TestCopyNeverClaimsAllPairSimilarity:
    def test_definition_states_the_connected_caveat(self) -> None:
        assert "connected through one or more strong" in FAMILY_DEFINITION
        assert "Not every pair inside a larger family must" in FAMILY_DEFINITION

    def test_chained_family_copy_reflects_connectivity(self) -> None:
        _, group = _chain(3)
        line = interpretation(group, FamilyStructure(group), concentration=0.0)
        assert line not in (
            "High textual similarity — reviews read as near-copies of each other.",
            "High textual similarity across the family.",
        )
        assert "direct links" in line
        assert not any(claim in line for claim in _ALL_PAIR_CLAIMS)

    def test_complete_family_may_use_the_strong_wording(self) -> None:
        # A *lexical* complete pair is what the strong wording describes: the
        # texts genuinely read as near-copies.
        reviews = [_review(0, _MIXED_TEXTS[0], place="mix"), _review(1, _MIXED_TEXTS[1], place="mix")]
        group = _detect(reviews, _chain_embeddings(2, _MIXED_GAP_DEG))
        structure = FamilyStructure(group)
        assert structure.by_kind["near"] == 1
        line = interpretation(group, structure, concentration=0.0)
        assert line == "High textual similarity — reviews read as near-copies of each other."
        assert not structure.transitive

    def test_semantic_only_pair_uses_semantic_wording(self) -> None:
        # A semantic-only pair must not inherit the "near-copies" wording, even
        # when it is complete: similar meaning is not a claim about the text.
        reviews = [_review(0, _CHAIN_TEXTS[0]), _review(1, _CHAIN_TEXTS[1])]
        group = _detect(reviews, _chain_embeddings(2, _GAP_DEG))
        structure = FamilyStructure(group)
        assert structure.by_kind == {"exact": 0, "fuzzy": 0, "near": 0, "semantic": 1}
        line = interpretation(group, structure, concentration=0.0)
        assert (
            line
            == "Strong semantic similarity was detected between directly linked reviews."
        )
        assert "directly linked reviews" in line
        assert not any(claim in line for claim in _ALL_PAIR_CLAIMS)
        assert not structure.transitive

    def test_transitive_label_and_detail_are_neutral(self) -> None:
        assert TRANSITIVE_LABEL == "Contains transitive connections"
        assert "joined transitively through intermediate matches" in TRANSITIVE_DETAIL
        for wording in (TRANSITIVE_LABEL, TRANSITIVE_DETAIL):
            assert not any(
                bad in wording.lower() for bad in ("weak", "bad", "unreliable", "poor", "fail")
            )


class TestMemberEvidence:
    def test_evidence_lists_only_direct_links(self) -> None:
        _, group = _chain(3)
        evidence = member_evidence(group)
        assert [row[0] for row in evidence] == ["A", "B", "C"]
        by_label = {row[0]: row for row in evidence}
        # A is linked to B only — never to C.
        assert by_label["A"][2] == 1
        assert [r[0] for r in by_label["A"][3]] == ["B"]
        # C is linked to B only — never to A.
        assert by_label["C"][2] == 1
        assert [r[0] for r in by_label["C"][3]] == ["B"]
        # B is the bridge: linked to both.
        assert by_label["B"][2] == 2
        assert [r[0] for r in by_label["B"][3]] == ["A", "C"]

    def test_evidence_counts_never_exceed_possible_links(self) -> None:
        _, group = _chain(5)
        for _label, _rid, n_links, rows in member_evidence(group):
            assert len(rows) == n_links
            assert n_links <= len(group.review_ids) - 1

    def test_note_explains_a_missing_line(self) -> None:
        assert "no direct link was detected" in EVIDENCE_NOTE
        assert "intermediate member" in EVIDENCE_NOTE

    def test_labels_follow_member_order(self) -> None:
        _, group = _chain(3)
        labels = member_labels(group)
        assert list(labels) == list(group.review_ids)
        assert list(labels.values()) == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Exact duplicate family
# ---------------------------------------------------------------------------


class TestExactFamily:
    def test_exact_family_counts_reviews_and_links_separately(self) -> None:
        reviews = [_review(i, _EXACT_TEXT, place="exact") for i in range(3)]
        group = _detect(reviews, _chain_embeddings(3, _GAP_DEG))
        st = FamilyStructure(group)
        assert group.exact_count == 3  # review count, as documented
        assert st.by_kind["exact"] == 3  # C(3, 2) pairs
        assert st.links == 3
        assert st.possible == 3
        assert st.transitive is False

    def test_exact_family_signal_is_unchanged(self) -> None:
        reviews = [_review(i, _EXACT_TEXT, place="exact") for i in range(3)]
        group = _detect(reviews, _chain_embeddings(3, _GAP_DEG))
        assert group.signals == ["3 exact duplicates"]

    def test_exact_family_edge_scores_are_one(self) -> None:
        reviews = [_review(i, _EXACT_TEXT, place="exact") for i in range(3)]
        group = _detect(reviews, _chain_embeddings(3, _GAP_DEG))
        assert all(e[2] == "exact" and e[3] == 1.0 for e in group.edges)


# ---------------------------------------------------------------------------
# Mixed lexical + semantic family
# ---------------------------------------------------------------------------


class TestMixedFamily:
    def test_lexical_and_semantic_links_coexist(self, mixed_family) -> None:
        st = FamilyStructure(mixed_family)
        assert st.by_kind["near"] == 1
        assert st.by_kind["semantic"] == 1
        assert st.by_kind["exact"] == 0
        assert mixed_family.near_count == 1
        assert mixed_family.semantic_count == 1

    def test_mixed_family_is_transitive(self, mixed_family) -> None:
        st = FamilyStructure(mixed_family)
        assert st.n == 3
        assert st.possible == 3
        assert st.links == 2
        assert st.transitive is True

    def test_mixed_evidence_labels_each_kind(self, mixed_family) -> None:
        rows = {label: data for label, _rid, _n, data in member_evidence(mixed_family)}
        kinds = {kind for pairs in rows.values() for _other, kind, _score in pairs}
        assert kinds == {"near", "semantic"}


# ---------------------------------------------------------------------------
# Rating alignment is context, never membership
# ---------------------------------------------------------------------------


class TestRatingContext:
    def test_context_reports_range_spread_and_shared_share(self) -> None:
        reviews = [_review(i, _CHAIN_TEXTS[i], rating=r) for i, r in enumerate([5, 5, 3])]
        ctx = rating_context(reviews)
        assert ctx["available"] is True
        assert ctx["low"] == 3
        assert ctx["high"] == 5
        assert ctx["spread"] == 2
        assert ctx["identical_share"] == pytest.approx(2 / 3)

    def test_missing_ratings_report_unavailable(self) -> None:
        reviews = [_review(0, _CHAIN_TEXTS[0], rating=None)]
        ctx = rating_context(reviews)
        assert ctx["available"] is False
        assert "no ratings on record" in rating_line(ctx)

    def test_line_states_that_ratings_never_decide_membership(self) -> None:
        reviews = [_review(i, _CHAIN_TEXTS[i], rating=5) for i in range(2)]
        line = rating_line(rating_context(reviews))
        assert "separate from text matching" in line
        assert "never decides who is in a family" in line

    def test_ratings_do_not_change_group_membership(self) -> None:
        """The same texts with different ratings must yield the same family."""
        embeddings = _chain_embeddings(3, _GAP_DEG)
        identical = DuplicateDetector().detect(
            [_review(i, _CHAIN_TEXTS[i], rating=5, place="rate") for i in range(3)],
            embeddings=embeddings,
        )
        varied = DuplicateDetector().detect(
            [_review(i, _CHAIN_TEXTS[i], rating=r, place="rate") for i, r in enumerate([5, 3, 4])],
            embeddings=embeddings,
        )
        assert [sorted(g.review_ids) for g in identical] == [
            sorted(g.review_ids) for g in varied
        ]
        assert len(varied[0].review_ids) == 3


# ---------------------------------------------------------------------------
# Groups rebuilt without edge detail must not render misleading zeros
# ---------------------------------------------------------------------------


class TestStoredGroupWithoutEdges:
    def test_missing_edges_render_as_unavailable_not_zero(self) -> None:
        from reviewscope.analysis.duplicates import DuplicateGroup

        stored = DuplicateGroup(
            group_id=1,
            review_ids=["r00", "r01", "r02"],
            exact_count=2,
            fuzzy_count=1,
            near_count=0,
            semantic_count=0,
            avg_similarity=0.91,
        )
        st = FamilyStructure(stored)
        assert st.has_structure is False
        assert st.links is None
        assert st.density is None
        assert st.by_kind is None
        assert st.transitive is False  # no claim either way
        assert st.sparse is False
        assert "pair-level structure not stored" in st.summary()


# ---------------------------------------------------------------------------
# Rendering: the transitive indication must actually reach the screen
# ---------------------------------------------------------------------------

_PLACE = "chain_place"


def _build_db(path: Path, texts: list[str], gap_deg: float) -> str:
    reviews = [
        NormalizedReview(
            review_id=f"{_PLACE}-{i}",
            place_id=_PLACE,
            place_name="Chain Cafe",
            place_category="cafe",
            reviewer_id=f"user-{i}",
            rating=[5, 5, 3][i % 3],
            text=texts[i],
            published_at=f"2026-08-{1 + i:02d}T10:00:00",
            city="Moscow",
            region="Moscow",
            country="Russia",
        )
        for i in range(len(texts))
    ]
    model = CONFIG.embedding.model_name
    with DuckDBStore(db_path=path, read_only=False) as store:
        store.ingest(reviews)
        vectors = _chain_embeddings(len(texts), gap_deg)
        store.store_cached_embeddings(
            [(r.review_id, r.fingerprint(), model, vectors[i].tolist()) for i, r in enumerate(reviews)]
        )
    return str(path)


def _navigate(at: AppTest, page: str) -> None:
    at.sidebar.radio[0].set_value(page)
    at.run()


@pytest.fixture(scope="module")
def chain_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("chain") / "chain.duckdb"
    return _build_db(path, _CHAIN_TEXTS[:3], _GAP_DEG)


class TestRendersTransitiveFamily:
    def test_transitive_indication_renders(self, chain_db, monkeypatch) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", chain_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        _navigate(at, "Duplicates")
        assert not list(at.exception), [e.value for e in at.exception]
        markdown = "\n".join(m.value for m in at.markdown)
        captions = " ".join(c.value for c in at.caption)
        assert TRANSITIVE_LABEL in markdown
        assert FAMILY_DEFINITION in markdown or FAMILY_DEFINITION in captions

    def test_rendered_copy_never_claims_all_pair_similarity(
        self, chain_db, monkeypatch
    ) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", chain_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        _navigate(at, "Duplicates")
        assert not list(at.exception), [e.value for e in at.exception]
        rendered = "\n".join(m.value for m in at.markdown) + "\n" + " ".join(
            c.value for c in at.caption
        )
        for claim in _ALL_PAIR_CLAIMS:
            assert claim not in rendered
        # ...and the connected-component caveat must be on screen.
        assert "Not every pair inside a larger family must directly pass" in rendered
        assert "connected through one or more strong" in rendered
        assert "direct links of 3 possible pairs" in rendered

    def test_relationship_evidence_is_progressively_disclosed(
        self, chain_db, monkeypatch
    ) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", chain_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        _navigate(at, "Duplicates")
        assert not list(at.exception), [e.value for e in at.exception]
        expanders = [e.label for e in at.expander]
        assert "Why these reviews are in this family" in expanders
        markdown = "\n".join(m.value for m in at.markdown)
        assert TRANSITIVE_LABEL in markdown

    def test_page_uses_family_terminology(self, chain_db, monkeypatch) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", chain_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        _navigate(at, "Duplicates")
        assert not list(at.exception), [e.value for e in at.exception]
        headers = " ".join(h.value for h in at.header)
        assert "Repeated-text families" in headers
        metrics = {m.label for m in at.metric}
        assert "Repeated-text families" in metrics
        markdown = "\n".join(m.value for m in at.markdown)
        assert "**Semantic similarity family** · 3 reviews" in markdown
