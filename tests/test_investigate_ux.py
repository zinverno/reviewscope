"""Phase 18.1 — case investigation UX (graph, comparison, compact text).

These tests pin the refined workspace contract:

* the relationship graph draws exactly the *stored* detector edges — never a
  transitive or position-derived pair — one trace per detection level;
* layout is deterministic for a given family and geometric only;
* selected review / comparison target stay in sync, comparison defaults to a
  direct neighbour, and a family switch resets both;
* review text renders as escaped, compact Markdown cards (no oversized
  ``st.text_area``, no unescaped untrusted text);
* a semantically-only family keeps precise wording (no "near-copies");
* summary metrics, technical-details expander and the "no direct link" message
  all reach the screen.

Page flows run through ``AppTest`` as elsewhere (SPEC.md §39).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from reviewscope.analysis.duplicates import DuplicateGroup
from reviewscope.config import CONFIG
from reviewscope.models.review import NormalizedReview
from reviewscope.storage import DuckDBStore
from reviewscope.ui import common
from reviewscope.ui.duplicates import member_labels
from reviewscope.ui.investigate import (
    COMPARE_KEY,
    GRAPH_KEY,
    MEMBER_KEY,
    SWITCHER_KEY,
    build_graph_figure,
    default_comparison,
    direct_neighbors,
    edges_frame,
    escape_review_text,
    node_positions,
)

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

EXACT_TEXT = "Отличное место, превосходный кофе и внимательный персонал каждый раз."
NEAR_A = "Быстрое обслуживание и вкусные десерты по приятным ценам."
NEAR_B = "Быстрое обслуживание и вкусные десерты по приятным ценам!"

CHAIN_TEXTS = [
    "Great coffee and the staff were genuinely friendly that morning.",
    "Excellent espresso with warm service and a quiet corner seat.",
    "Lovely pastries, calm atmosphere and easy parking out front.",
]

LONG_TEXT = (
    "Очень уютное заведение около парка: кофе находит свою крепость, персонал "
    "помнит постоянных гостей по именам, а по выходным играет живая музыка, "
    "поэтому вечером сложно найти свободный столик."
) + " " + (
    "В меню есть и завтраки до полудня, и несложные ужины после работы, а "
    "летняя веранда выходит прямо на аллею, где приятно сидеть вечером."
) + " " + (
    "Гостям нравится, что счёт приносят быстро и без напоминаний, а вода на "
    "стойке всегда свежая и прохладная, что редкость для этого района."
)


def _review(review_id: str, place_id: str, text: str, *, day: int = 1) -> NormalizedReview:
    return NormalizedReview(
        review_id=review_id,
        place_id=place_id,
        place_name="Fixture Place",
        place_category="cafe",
        reviewer_id=f"user-{review_id}",
        rating=5,
        text=text,
        published_at=f"2026-09-{day:02d}T10:00:00",
    )


def _chain_embeddings(n: int, gap_deg: float, dim: int = 384) -> np.ndarray:
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


def _family_reviews() -> list[NormalizedReview]:
    return [
        _review("dup-0", "dupcafe", EXACT_TEXT, day=1),
        _review("dup-1", "dupcafe", EXACT_TEXT, day=1),
        _review("dup-2", "dupcafe", EXACT_TEXT, day=2),
        _review("dup-3", "dupcafe", EXACT_TEXT, day=2),
        _review("dup-4", "dupcafe", NEAR_A, day=3),
        _review("dup-5", "dupcafe", NEAR_B, day=3),
        _review("clean-0", "cleancafe", "Совсем другое место совсем других слов тут нет."),
    ]


@pytest.fixture(scope="module")
def family_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("uxfamily") / "family.duckdb"
    return _build_db(path, _family_reviews())


@pytest.fixture(scope="module")
def chain_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("uxchain") / "chain.duckdb"
    reviews = [_review(f"chain-{i}", "chain", CHAIN_TEXTS[i], day=i + 1) for i in range(3)]
    return _build_db(path, reviews, _chain_embeddings(3, 20.0))


@pytest.fixture(scope="module")
def long_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("uxlong") / "long.duckdb"
    reviews = [
        _review("long-0", "longcafe", LONG_TEXT, day=1),
        _review("long-1", "longcafe", LONG_TEXT, day=1),
    ]
    return _build_db(path, reviews)


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


def _rendered(at: AppTest) -> str:
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption]
    parts += [h.value for h in at.header]
    parts += [s.value for s in at.subheader]
    parts += [i.value for i in at.info]
    parts += [m.label for m in at.metric]
    return "\n".join(parts)


def _graph_spec(at: AppTest, key: str = GRAPH_KEY) -> dict:
    charts = [p for p in at.get("plotly_chart") if p.key == key]
    assert charts, "workspace relationship graph not rendered"
    return json.loads(charts[0].proto.spec)


def _edge_count(spec: dict) -> int:
    total = 0
    for trace in spec["data"]:
        if trace.get("mode") == "lines":
            points = [p for p in trace.get("x", []) if p is not None]
            total += len(points) // 2
    return total


def _node_trace(spec: dict) -> dict:
    nodes = [t for t in spec["data"] if t.get("mode") == "markers+text"]
    assert nodes
    return nodes[0]


# ---------------------------------------------------------------------------
# Pure helpers — escape
# ---------------------------------------------------------------------------


class TestEscapeReviewText:
    def test_plain_text_is_unchanged(self) -> None:
        assert escape_review_text(EXACT_TEXT) == EXACT_TEXT

    def test_markdown_metacharacters_are_escaped(self) -> None:
        escaped = escape_review_text("*bold* `code` |pipe| and [link](x) <b>!  # hash_x")
        assert "*bold*" not in escaped
        assert "\\*bold\\*" in escaped
        assert "`code`" not in escaped
        assert "<b>" not in escaped
        assert "[link](x)" not in escaped

    def test_newlines_become_hard_breaks(self) -> None:
        assert "\n" not in escape_review_text("first\nsecond").replace("  \n", "  |")


# ---------------------------------------------------------------------------
# Pure helpers — graph layout and edges
# ---------------------------------------------------------------------------


class TestGraphGeometry:
    def _chain(self) -> DuplicateGroup:
        return DuplicateGroup(
            group_id=1,
            review_ids=["r0", "r1", "r2"],
            edges=[("r0", "r1", "semantic", 0.94), ("r1", "r2", "semantic", 0.90)],
            avg_similarity=0.92,
        )

    def test_positions_are_in_unit_square(self) -> None:
        positions = node_positions(self._chain())
        assert set(positions) == {"r0", "r1", "r2"}
        assert all(0.0 < v < 1.0 for p in positions.values() for v in p)

    def test_positions_are_deterministic_under_member_order(self) -> None:
        group = self._chain()
        forward = node_positions(group)
        group.review_ids = ["r2", "r1", "r0"]
        backward = node_positions(group)
        assert forward["r0"] == backward["r0"]

    def test_layout_uses_stored_edges_only(self) -> None:
        # r0/r2 are similar-sounding, but no A–C edge was stored; geometry must
        # not invent one.
        neighbors = direct_neighbors(self._chain(), "r0")
        assert neighbors == ["r1"]
        assert direct_neighbors(self._chain(), "r2") == ["r1"]

    def test_edges_frame_counts_matches_edges(self) -> None:
        labels = member_labels(self._chain())
        frame = edges_frame(self._chain(), labels)
        assert len(frame) == 2
        assert list(frame.columns) == ["From", "To", "Link kind", "Similarity"]

    def test_default_comparison_prefers_a_direct_neighbour(self) -> None:
        assert default_comparison(self._chain(), "r0") == "r1"
        assert default_comparison(self._chain(), "r2") == "r1"

    def test_default_comparison_never_returns_self(self) -> None:
        group = DuplicateGroup(group_id=2, review_ids=["solo"])
        assert default_comparison(group, "solo") is None

    def test_large_dense_family_stays_finite_and_deterministic(self) -> None:
        ids = [f"r{i:02d}" for i in range(19)]
        edges = []
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                if (i + int(b[1:])) % 5 != 0:
                    edges.append((a, b, "semantic", 0.9))
        group = DuplicateGroup(group_id=9, review_ids=ids, edges=edges, avg_similarity=0.9)
        positions = node_positions(group)
        assert set(positions) == set(ids)
        flat = [v for p in positions.values() for v in p]
        assert len(flat) == 2 * len(ids)
        assert all(v == v for v in flat), "layout produced NaN"
        assert all(0.0 < v < 1.0 for v in flat), "layout escaped the unit square"
        group.review_ids = list(reversed(ids))
        again = node_positions(group)
        assert again["r00"] == positions["r00"]


class TestGraphFigure:
    def test_one_edge_trace_per_detection_level(self) -> None:
        group = DuplicateGroup(
            group_id=1,
            review_ids=["r0", "r1", "r2"],
            edges=[
                ("r0", "r1", "exact", 1.0),
                ("r1", "r2", "semantic", 0.91),
            ],
            avg_similarity=0.95,
            exact_count=2,
        )
        labels = member_labels(group)
        figure = build_graph_figure(group, labels, node_positions(group), "r1", {"r0", "r2"})
        names = {t.name for t in figure.data}
        assert names == {"identical text", "semantic similarity", None}

    def test_every_edge_become_a_line_segment(self) -> None:
        group = DuplicateGroup(
            group_id=1,
            review_ids=["r0", "r1", "r2", "r3"],
            edges=[("r0", "r1", "exact", 1.0), ("r2", "r3", "exact", 1.0)],
            avg_similarity=0.95,
            exact_count=4,
        )
        labels = member_labels(group)
        figure = build_graph_figure(group, labels, node_positions(group), "r0", {"r1"})
        line_traces = [t for t in figure.data if t.mode == "lines"]
        assert len(line_traces) == 1
        non_null = [x for x in line_traces[0].x if x is not None]
        assert len(non_null) == 4  # two edges × two endpoints


# ---------------------------------------------------------------------------
# Graph on screen
# ---------------------------------------------------------------------------


class TestGraphOnScreen:
    def test_exact_family_draws_all_stored_edges(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        spec = _graph_spec(at)
        assert _edge_count(spec) == 6  # C(4, 2) same-text links
        line_traces = [t for t in spec["data"] if t.get("mode") == "lines"]
        assert [t.get("name") for t in line_traces] == ["identical text"]
        nodes = _node_trace(spec)
        assert len(nodes["x"]) == 4

    def test_selected_member_is_highlighted(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        spec = _graph_spec(at)
        sizes = _node_trace(spec)["marker"]["size"]
        assert sizes == [26, 16, 16, 16]  # selected + three direct neighbours
        symbols = _node_trace(spec)["marker"]["symbol"]
        assert symbols == ["star", "circle", "circle", "circle"]

    def test_chain_graph_never_invents_the_missing_pair(self, chain_db, monkeypatch) -> None:
        at = _app(chain_db, monkeypatch)
        _navigate(at, "Duplicates")
        at.button(key="open_family_ws_0").click()
        _run(at)
        spec = _graph_spec(at)
        assert _edge_count(spec) == 2  # A–B and B–C, never A–C
        line_traces = [t for t in spec["data"] if t.get("mode") == "lines"]
        assert [t.get("name") for t in line_traces] == ["semantic similarity"]
        nodes = _node_trace(spec)
        assert len(nodes["x"]) == 3


# ---------------------------------------------------------------------------
# Selection and comparison sync
# ---------------------------------------------------------------------------


class TestSelectionSync:
    def test_comparison_defaults_to_a_direct_neighbour(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        assert at.selectbox(key=MEMBER_KEY).value == "dup-0"
        assert at.selectbox(key=COMPARE_KEY).value == "dup-1"
        assert at.session_state[COMPARE_KEY] == "dup-1"

    def test_switching_member_resets_compare_if_identical(
        self, family_db, monkeypatch
    ) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        at.selectbox(key=COMPARE_KEY).set_value("dup-0")
        _run(at)
        assert at.session_state[COMPARE_KEY] == "dup-1"  # reset to a neighbour

    def test_member_change_keeps_a_valid_compare(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        at.selectbox(key=COMPARE_KEY).set_value("dup-2")
        at.selectbox(key=MEMBER_KEY).set_value("dup-1")
        _run(at)
        assert at.session_state[COMPARE_KEY] == "dup-2"
        assert at.session_state[MEMBER_KEY] == "dup-1"

    def test_family_switch_resets_member_and_compare(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        assert at.session_state[MEMBER_KEY] == "dup-0"
        second_key = "dupcafe::dup-4,dup-5"
        at.selectbox(key=SWITCHER_KEY).set_value(second_key)
        _run(at)
        assert at.session_state[MEMBER_KEY] == "dup-4"
        assert at.session_state[COMPARE_KEY] == "dup-5"

    def test_pair_without_direct_link_is_flagged(self, chain_db, monkeypatch) -> None:
        at = _app(chain_db, monkeypatch)
        _navigate(at, "Duplicates")
        at.button(key="open_family_ws_0").click()
        _run(at)
        at.selectbox(key=MEMBER_KEY).set_value("chain-2")
        _run(at)
        at.selectbox(key=COMPARE_KEY).set_value("chain-0")
        _run(at)
        rendered = _rendered(at)
        assert "No direct detector relationship was recorded between these two reviews." in rendered


# ---------------------------------------------------------------------------
# Compact text rendering
# ---------------------------------------------------------------------------


class TestCompactText:
    def test_short_review_renders_in_full_as_markdown(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        assert not at.text_area
        rendered = _rendered(at)
        assert escape_review_text(EXACT_TEXT) in rendered
        assert "Show full text" not in rendered  # short text needs no preview

    def test_long_review_is_previewed_with_full_text_on_demand(
        self, long_db, monkeypatch
    ) -> None:
        at = _app(long_db, monkeypatch)
        _navigate(at, "Duplicates", place="longcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        assert escape_review_text(LONG_TEXT) in _rendered(at)
        assert "…" in " ".join(m.value for m in at.markdown)
        expanders = [e.label for e in at.expander]
        assert "Show full text" in expanders


# ---------------------------------------------------------------------------
# Precise language
# ---------------------------------------------------------------------------


class TestPreciseLanguage:
    def test_semantic_only_chain_never_claims_near_copies(self, chain_db, monkeypatch) -> None:
        at = _app(chain_db, monkeypatch)
        _navigate(at, "Duplicates")
        at.button(key="open_family_ws_0").click()
        _run(at)
        rendered = _rendered(at)
        assert (
            "Strong semantic similarity was detected along the family's direct links."
            in rendered
        )
        assert "near-copies of each other" not in rendered


# ---------------------------------------------------------------------------
# Summary and technical details
# ---------------------------------------------------------------------------


class TestHierarchy:
    def test_summary_metrics_reach_the_screen(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        metrics = {m.label: m.value for m in at.metric}
        assert metrics["Reviews"] == "4"
        assert metrics["Distinct reviewers"] == "4"
        assert metrics["Direct links"] == "6 of 6"
        assert "100%" in metrics["Link density"]

    def test_technical_details_exposes_diagnostics(self, family_db, monkeypatch) -> None:
        at = _app(family_db, monkeypatch)
        _navigate(at, "Duplicates", place="dupcafe")
        at.button(key="open_family_ws_0").click()
        _run(at)
        labels = [e.label for e in at.expander]
        assert "Technical details" in labels
        rendered = _rendered(at)
        assert "Relationship matrix (direct links)" in rendered
