"""Case investigation workspace: open one repeated-text family and stay with it.

Reached from a family card on the Duplicates page or from Discover's place
drill-down, this sub-view presents a single repeated-text family: a summary,
an interactive relationship graph drawn only from the pairs the detector linked
*directly*, a side-by-side comparison of two members, the members table, and
technical details behind an expander.

Everything derives from an existing ``DuplicateGroup``: membership, ``edges``
and the presentation helpers of :mod:`reviewscope.ui.duplicates`. No score,
label, threshold or grouping rule is added here, and the wording stays neutral
(SPEC.md §29, §36): the view reports detector output and never a verdict.

Graph layout is force-directed, seeded from the family's member identities so
the same family renders in the same place on every rerun; edge *presence* never
feeds back into grouping or scoring, and edges are never inferred from node
positions. Keyword escapes keep untrusted review text readable as plain text.

The selection lives in ``st.session_state`` under this module's keys and is
identified by the family's *member identities* (place id + sorted review ids),
never by a positional index or a run-local group id, so it survives reruns and
page navigation. A dataset switch invalidates it.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

import pandas as pd
import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine

from .common import info_state
from .duplicates import (
    EVIDENCE_NOTE,
    FAMILY_DEFINITION,
    KIND_LABELS,
    TRANSITIVE_DETAIL,
    TRANSITIVE_LABEL,
    FamilyStructure,
    family_label,
    group_stats,
    interpretation,
    member_evidence,
    member_labels,
)

if TYPE_CHECKING:
    from plotly.graph_objects import Figure

#: Session-state key: identity of the selected family (see :func:`family_identity`).
FAMILY_KEY = "investigation_family"
#: Session-state key: dataset (db path) the selection belongs to.
FAMILY_DB_KEY = "investigation_family_db"
#: Session-state key: workspace-versus-list mode.
OPEN_KEY = "investigation_family_open"

#: Widget keys, exported so tests can address them directly.
SWITCHER_KEY = "investigation_family_switcher"
MEMBER_KEY = "investigation_member"
COMPARE_KEY = "investigation_comparison"
BACK_KEY = "family_ws_back"
RESUME_KEY = "family_ws_resume"
CLEAR_KEY = "family_ws_clear"
MEMBERS_TABLE_KEY = "family_ws_members"
MATRIX_KEY = "family_ws_matrix"
GRAPH_KEY = "family_ws_graph"

#: Neutral framing of what this view is for.
WORKSPACE_INTRO = (
    "Inspect one repeated-text family: its members, the pairs the detector "
    "linked directly, and where the family connects only through an "
    "intermediate review. This view reports detector output; it does not "
    "assert fraud."
)


# ---------------------------------------------------------------------------
# Family identity (pure)
# ---------------------------------------------------------------------------


def family_identity(place_id: str, review_ids) -> str:
    """Stable identity of one family: place id plus sorted member review ids.

    Positional indexes and run-local group ids are deliberately avoided, so
    the same family keeps the same identity across reruns, page navigation
    and a re-run of the detector on the same corpus.
    """
    return f"{place_id}::" + ",".join(sorted(review_ids))


def family_options(groups, place_id: str) -> list[tuple[str, object]]:
    """``(key, group)`` pairs for every family of this place, largest first.

    The sort uses only intrinsic family facts (size, average link similarity)
    plus the identity string as a final tiebreaker, so the order is
    deterministic across reruns.
    """
    options = [
        (family_identity(place_id, g.review_ids), g) for g in groups if len(g.review_ids) >= 2
    ]
    options.sort(key=lambda item: (-len(item[1].review_ids), -item[1].avg_similarity, item[0]))
    return options


def option_label(group) -> str:
    """One-line family label: size, member span and link facts — never a verdict."""
    labels = member_labels(group)
    structure = FamilyStructure(group)
    first = labels.get(group.review_ids[0], "?")
    last = labels.get(group.review_ids[-1], "?")
    span = first if first == last else f"{first}–{last}"
    if structure.has_structure:
        facts = f"{structure.links} direct links · similarity {group.avg_similarity:.2f}"
    else:
        facts = "pair-level structure not stored"
    return f"{len(group.review_ids)} reviews ({span}) · {facts}"


def kind_phrase(kind: str, score: float) -> str:
    """Neutral description of one detected link, with its similarity score."""
    if kind == "exact":
        return "identical text"
    if kind == "semantic":
        return f"semantic similarity {score:.2f}"
    return f"{KIND_LABELS.get(kind, kind)} · similarity {score:.2f}"


# ---------------------------------------------------------------------------
# Derived tables (pure)
# ---------------------------------------------------------------------------


_MD_SPECIALS = re.compile(r"([\\`*_{}\[\]<>()#+\-|])")

_REVIEW_PREVIEW_LIMIT = 280


def escape_review_text(text: str, *, limit: int | None = None) -> str:
    """Escape review text so it renders as plain text in ``st.markdown``.

    Review text is untrusted input, so every Markdown/HTML metacharacter is
    escaped before it reaches the page, and each newline becomes a hard line
    break so the review keeps its own shape.
    """
    if limit is not None and len(text) > limit:
        text = text[:limit]
    return _MD_SPECIALS.sub(r"\\\1", text).replace("\n", "  \n")


def render_review_text(text: str, *, entry_id: str = "") -> None:
    """Render one review's text compactly (preview + full text on demand)."""
    escaped = _MD_SPECIALS.sub(r"\\\1", text).replace("\n", "  \n")
    if len(escaped) <= _REVIEW_PREVIEW_LIMIT:
        st.markdown(escaped)
        return
    st.markdown(escaped[:_REVIEW_PREVIEW_LIMIT] + "…")
    with st.expander("Show full text", expanded=False):
        st.markdown(escaped)


def member_frame(group, by_id: dict) -> pd.DataFrame:
    """One row per member: identity, context and direct-link facts.

    Link counts are *direct* links only. "not stored" marks a family rebuilt
    from a payload that never carried its edge list — a missing capability is
    never rendered as an observed zero.
    """
    labels = member_labels(group)
    structure = FamilyStructure(group)
    evidence = {rid: (n, rows) for _label, rid, n, rows in member_evidence(group)}
    n_members = len(group.review_ids)
    rows = []
    for rid in group.review_ids:
        review = by_id.get(rid)
        n_links, link_rows = evidence.get(rid, (0, []))
        if structure.has_structure:
            links = f"{n_links} of {max(n_members - 1, 0)}"
            kinds = ", ".join(sorted({KIND_LABELS[k] for _other, k, _score in link_rows}))
            kinds = kinds or "—"
        else:
            links = "not stored"
            kinds = "not stored"
        rows.append(
            {
                "Member": labels.get(rid, "?"),
                "Review": rid,
                "Reviewer": (review.reviewer_id if review else None) or "—",
                "Rating": f"{review.rating}★" if review and review.rating is not None else "—",
                "Published": (
                    (review.published_at or "")[:10]
                    if review and review.published_at
                    else "—"
                ),
                "Direct links": links,
                "Link kinds": kinds,
            }
        )
    return pd.DataFrame(rows)


def relationship_frame(group) -> pd.DataFrame | None:
    """Member × member matrix of *direct* links; ``—`` marks no direct link.

    ``None`` when the group carries no edge list (a payload rebuilt without
    edge detail), so callers can say the capability is missing rather than
    render an empty matrix.
    """
    if not group.edges:
        return None
    labels = member_labels(group)
    names = [labels[rid] for rid in group.review_ids]
    frame = pd.DataFrame("—", index=names, columns=names, dtype=object)
    for position in range(len(names)):
        frame.iat[position, position] = ""
    for a, b, kind, score in group.edges:
        if a not in labels or b not in labels:
            continue
        cell = kind_phrase(kind, score)
        frame.at[labels[a], labels[b]] = cell
        frame.at[labels[b], labels[a]] = cell
    return frame


def edges_frame(group, labels: dict) -> pd.DataFrame:
    """One row per *stored* edge: labels on both ends, kind and similarity."""
    rows = []
    for a, b, kind, score in group.edges:
        rows.append(
            {
                "From": labels.get(a, a),
                "To": labels.get(b, b),
                "Link kind": KIND_LABELS.get(kind, kind),
                "Similarity": score,
            }
        )
    return pd.DataFrame(rows, columns=["From", "To", "Link kind", "Similarity"])


def direct_neighbors(group, rid: str) -> list[str]:
    """Members directly linked to ``rid``, in the family's label order.

    Only stored pairs are consulted: a pair that was never detected never
    becomes an edge here, no matter how similar the two texts read to a human.
    """
    order = {member: i for i, member in enumerate(group.review_ids)}
    neighbors = {other for a, b, _, _ in group.edges if rid in (a, b) for other in (a, b) if other != rid}
    return sorted(neighbors, key=order.get)


def node_positions(group) -> dict[str, tuple[float, float]]:
    """Deterministic layout from the *stored* edges only.

    Tiny families use compact fixed layouts so they read clearly instead of
    degenerating into one continuous line: two members sit side by side, three
    form an open V with the middle member at the point. Larger families use a
    bounded force-directed layout (Fruchterman-Ringold) where forces are
    summed into per-node accumulators before a single simultaneous move per
    iteration, so the same family renders in the same place on every rerun,
    whatever the exact detector run order or payload member order was. Node
    coordinates live in [0, 1]² and are purely for display.
    """
    members = group.review_ids
    n = len(members)
    if n == 0:
        return {}
    if n == 1:
        return {members[0]: (0.5, 0.5)}
    ordered = sorted(members)
    if n == 2:
        return {
            ordered[0]: (0.25, 0.5),
            ordered[1]: (0.75, 0.5),
        }
    if n == 3:
        return {
            ordered[0]: (0.18, 0.8),
            ordered[1]: (0.5, 0.2),
            ordered[2]: (0.82, 0.8),
        }
    positions = {}
    for i, rid in enumerate(ordered):
        angle = 2.0 * math.pi * i / n
        positions[rid] = (0.15 * math.cos(angle), 0.15 * math.sin(angle))
    ideal = 0.35 / math.sqrt(max(n, 2))
    iterations = 60
    for step in range(iterations):
        delta = {rid: [0.0, 0.0] for rid in ordered}
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                ax, ay = positions[a]
                bx, by = positions[b]
                dx, dy = ax - bx, ay - by
                dist = math.hypot(dx, dy) or 0.01
                push = ideal * ideal / dist
                ux, uy = dx / dist, dy / dist
                delta[a][0] += ux * push
                delta[a][1] += uy * push
                delta[b][0] -= ux * push
                delta[b][1] -= uy * push
        for a, b, _k, _s in group.edges:
            ax, ay = positions[a]
            bx, by = positions[b]
            dx, dy = ax - bx, ay - by
            dist = math.hypot(dx, dy) or 0.01
            pull = dist * dist / ideal
            ux, uy = dx / dist, dy / dist
            delta[a][0] -= ux * pull
            delta[a][1] -= uy * pull
            delta[b][0] += ux * pull
            delta[b][1] += uy * pull
        cap = 0.25 * (1.0 - step / iterations)
        for rid in ordered:
            dx, dy = delta[rid]
            dist = math.hypot(dx, dy)
            if dist <= 0.0:
                continue
            scale = min(dist, cap) / dist
            x, y = positions[rid]
            positions[rid] = (
                max(-1.0, min(1.0, x + dx * scale)),
                max(-1.0, min(1.0, y + dy * scale)),
            )
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    width = max(xs) - min(xs) or 1.0
    height = max(ys) - min(ys) or 1.0
    return {
        rid: (0.08 + 0.84 * (positions[rid][0] - min(xs)) / width,
              0.08 + 0.84 * (positions[rid][1] - min(ys)) / height)
        for rid in members
    }


def default_comparison(group, rid: str) -> str | None:
    """Deterministic comparison target: first direct neighbour, else first member.

    A family with a single member has nothing to compare against and returns
    ``None``.
    """
    if len(group.review_ids) < 2:
        return None
    neighbours = direct_neighbors(group, rid)
    if neighbours:
        return neighbours[0]
    return next(member for member in group.review_ids if member != rid)


def _graph_kind_styles() -> tuple[list[str], dict, dict, dict]:
    order = ("exact", "fuzzy", "near", "semantic")
    names = {
        "exact": "identical text",
        "fuzzy": "fuzzy match",
        "near": "near duplicate",
        "semantic": "semantic similarity",
    }
    colors = {
        "exact": "#2563eb",
        "fuzzy": "#16a34a",
        "near": "#d97706",
        "semantic": "#8b5cf6",
    }
    dashes = {
        "exact": "solid",
        "fuzzy": "dash",
        "near": "dot",
        "semantic": "longdash",
    }
    return order, names, colors, dashes


def build_graph_figure(
    group,
    labels: dict,
    positions: dict,
    selected: str,
    neighbors: set,
) -> Figure:
    """One edge trace per detection level, plus a member node trace.

    Exactly the family's stored ``edges`` become lines — nothing is inferred
    from positions or transitively invented. ``selected`` is the highlighted
    member and ``neighbors`` its direct neighbours; unrelated members stay
    visible but muted. Doubling a coordinate and joining with ``None`` keeps a
    single legend entry per detection level.
    """
    import plotly.graph_objects as go

    edge_order, names, colors, dashes = _graph_kind_styles()
    figure = go.Figure()
    present = [kind for kind in edge_order if any(edge[2] == kind for edge in group.edges)]
    for kind in present:
        xs, ys, text, custom = [], [], [], []
        for a, b, kind_edge, score in group.edges:
            if kind_edge != kind:
                continue
            xa, ya = positions[a]
            xb, yb = positions[b]
            xs += [xa, xb, None]
            ys += [ya, yb, None]
            text += [f"{labels.get(a, a)} → {labels.get(b, b)}", "", ""]
            custom += [[kind, score], [kind, score], [None, None]]
        figure.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines",
                name=names.get(kind, kind),
                line=dict(color=colors.get(kind, "#64748b"), dash=dashes.get(kind, "solid"), width=2),
                hoverinfo="skip",
                customdata=custom,
                legendgroup="edges",
            )
        )
    figure.add_trace(
        go.Scatter(
            x=[positions[rid][0] for rid in group.review_ids],
            y=[positions[rid][1] for rid in group.review_ids],
            mode="markers+text",
            text=[labels.get(rid, "?") for rid in group.review_ids],
            textposition="top center",
            textfont=dict(size=13, color="#334155"),
            marker=dict(
                size=[
                    26 if rid == selected else 16 if rid in neighbors else 11
                    for rid in group.review_ids
                ],
                color=[
                    "#f59e0b" if rid == selected else "#3b82f6" if rid in neighbors else "#cbd5e1"
                    for rid in group.review_ids
                ],
                symbol=[
                    "star" if rid == selected else "circle"
                    for rid in group.review_ids
                ],
                line=dict(width=2, color="#e2e8f0"),
            ),
            hovertext=[
                f"{labels.get(rid, '?')} · {rid}" + (" · selected" if rid == selected else " · direct neighbour" if rid in neighbors else "")
                for rid in group.review_ids
            ],
            hoverinfo="text",
            showlegend=False,
        )
    )
    figure.update_layout(
        height=300 if len(group.review_ids) <= 3 else 460,
        margin=dict(l=12, r=12, t=12, b=12),
        xaxis=dict(visible=False, range=[0, 1]),
        yaxis=dict(visible=False, range=[0, 1]),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hoverlabel=dict(font=dict(size=12)),
    )
    return figure


# ---------------------------------------------------------------------------
# Selection state
# ---------------------------------------------------------------------------


def clear_investigation() -> None:
    """Forget the selection entirely: mode, identity, dataset and widget values."""
    for key in (FAMILY_KEY, FAMILY_DB_KEY, OPEN_KEY, SWITCHER_KEY, MEMBER_KEY, COMPARE_KEY):
        st.session_state.pop(key, None)


def set_investigation(key: str, db_path: str) -> None:
    """Select ``key`` and open the workspace on the next render.

    Safe to call before the workspace widgets are instantiated (a card button
    on the list, or the Discover drill-down); the caller triggers the rerun.
    """
    st.session_state[FAMILY_KEY] = key
    st.session_state[FAMILY_DB_KEY] = db_path
    st.session_state[OPEN_KEY] = True
    st.session_state[SWITCHER_KEY] = key
    st.session_state.pop(MEMBER_KEY, None)
    st.session_state.pop(COMPARE_KEY, None)


def _drop_stale_dataset(db_path: str) -> None:
    """Invalidate any selection that was recorded for a different dataset."""
    stored = st.session_state.get(FAMILY_DB_KEY)
    if stored is not None and stored != db_path:
        clear_investigation()


def _valid_key(groups, place_id: str, db_path: str) -> str | None:
    """The selected family key when it still exists in this place, else ``None``."""
    key = st.session_state.get(FAMILY_KEY)
    if not isinstance(key, str) or not key:
        return None
    if st.session_state.get(FAMILY_DB_KEY) != db_path:
        return None
    keys = {k for k, _ in family_options(groups, place_id)}
    return key if key in keys else None


def begin_family_investigation(engine: AnalysisEngine, place_id: str, db_path: str) -> bool:
    """Select this place's largest repeated-text family for the workspace.

    Used by the Discover drill-down, which knows the place but not its
    families. Returns ``False`` when the place has no repeated-text family.
    """
    groups = [g for g in engine.analyze(place_id).duplicate_groups if len(g.review_ids) >= 2]
    options = family_options(groups, place_id)
    if not options:
        return False
    set_investigation(options[0][0], db_path)
    return True


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_resume_bar(groups, place_id: str, db_path: str) -> None:
    """Dormant-selection notice above the family list; no-op when none exists."""
    _drop_stale_dataset(db_path)
    if st.session_state.get(OPEN_KEY):
        return
    key = _valid_key(groups, place_id, db_path)
    if key is None:
        return
    group = dict(family_options(groups, place_id))[key]
    structure = FamilyStructure(group)
    info_state(
        "Investigation in progress",
        f"{family_label(structure)} with {len(group.review_ids)} reviews is selected on this place.",
        hint="Resume the workspace to keep inspecting it, or clear the selection.",
    )
    left, right = st.columns(2)
    if left.button("Resume investigation", key=RESUME_KEY, width="stretch"):
        set_investigation(key, db_path)
        st.rerun()
    if right.button("Clear selection", key=CLEAR_KEY, width="stretch"):
        clear_investigation()
        st.rerun()


def maybe_render_workspace(place_id: str, db_path: str, groups, by_id: dict) -> bool:
    """Render the workspace when a family selection is open.

    Returns ``True`` when the workspace took over the page; ``False`` leaves
    the family list to render. An invalid selection (place or dataset changed)
    is dropped so the page falls back to the list with no stale state.
    """
    _drop_stale_dataset(db_path)
    if not st.session_state.get(OPEN_KEY):
        return False
    key = _valid_key(groups, place_id, db_path)
    if key is None:
        clear_investigation()
        return False
    _render_workspace(place_id, db_path, family_options(groups, place_id), key, by_id)
    return True


def _render_workspace(
    place_id: str,
    db_path: str,
    options: list[tuple[str, object]],
    key: str,
    by_id: dict,
) -> None:
    lookup = dict(options)
    keys = [k for k, _ in options]
    if st.session_state.get(SWITCHER_KEY) not in keys:
        st.session_state[SWITCHER_KEY] = key

    if st.button("Back to family list", key=BACK_KEY):
        st.session_state[OPEN_KEY] = False
        st.rerun()

    st.subheader("Case investigation")
    st.caption(WORKSPACE_INTRO)
    st.caption(FAMILY_DEFINITION)

    chosen = str(
        st.selectbox(
            "Family to inspect",
            keys,
            key=SWITCHER_KEY,
            format_func=lambda k: option_label(lookup[k]),
            help="Switch to another repeated-text family of this place; the workspace stays open.",
        )
    )
    if chosen != st.session_state.get(FAMILY_KEY):
        st.session_state[FAMILY_KEY] = chosen
        st.session_state.pop(MEMBER_KEY, None)
        st.session_state.pop(COMPARE_KEY, None)
    group = lookup[chosen]

    structure = FamilyStructure(group)
    st.markdown(f"### {family_label(structure)} · {len(group.review_ids)} reviews")
    labels = member_labels(group)
    members = [by_id[rid] for rid in group.review_ids if rid in by_id]
    _render_summary(group, structure, members)
    selected, comparison = _render_graph_section(group, structure, labels, by_id)
    _render_pair_comparison(group, labels, by_id, structure, selected, comparison)
    _render_members(group, by_id)
    _render_technical_details(group, structure)


def _render_summary(group, structure, members) -> None:
    st.markdown("### Family summary")
    distinct_reviewers = [
        m.reviewer_id for m in members if m.reviewer_id and m.reviewer_id != "—"
    ]
    possible = structure.possible
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Reviews", len(group.review_ids))
    col2.metric(
        "Distinct reviewers",
        len(set(distinct_reviewers)) if distinct_reviewers else "—",
    )
    col3.metric(
        "Direct links",
        f"{structure.links} of {possible}" if structure.has_structure else "not stored",
    )
    col4.metric(
        "Link density",
        f"{structure.density:.0%}" if structure.has_structure else "—",
    )
    if structure.has_structure:
        st.caption(f"Similarity over direct links: {group.avg_similarity:.3f} avg")
    else:
        st.caption(structure.summary())
    if members:
        _avg_rating, when, concentration = group_stats(members)
        date_span = (
            f"{when[0]} → {when[-1]}" if len(when) > 1 else (when[0] if when else "no dates")
        )
        rating_bit = f"average rating {_avg_rating:.2f} · " if _avg_rating is not None else ""
        st.caption(
            f"{rating_bit}date span {date_span} · date concentration {concentration:.0%}"
        )
    else:
        concentration = 0.0
    st.markdown(interpretation(group, structure, concentration))

    if structure.transitive:
        st.markdown(f"**{TRANSITIVE_LABEL}**")
        if structure.sparse:
            st.caption(TRANSITIVE_DETAIL)
    st.markdown("---")
    if group.signals:
        st.caption("Signals: " + "; ".join(group.signals))
    if group.counter_signals:
        st.caption("Counter-signals: " + "; ".join(group.counter_signals))


def _render_graph_section(group, structure, labels, by_id: dict) -> tuple[str, str]:
    st.markdown("### Relationship graph")
    rids = group.review_ids
    if not rids:
        return "", ""
    if st.session_state.get(MEMBER_KEY) not in rids:
        st.session_state[MEMBER_KEY] = rids[0]

    def _member_label(rid: str) -> str:
        if rid not in by_id:
            return labels.get(rid, "?")
        return f"{labels.get(rid, '?')} · " + by_id[rid].text_or_empty()[:70]

    selected = str(
        st.selectbox(
            "Selected review",
            rids,
            key=MEMBER_KEY,
            format_func=_member_label,
            help="Highlight a member in the graph; it becomes the left card of the pair comparison.",
        )
    )
    neighbors = set(direct_neighbors(group, selected)) if structure.has_structure else set()
    ordered = [r for r in rids if r in neighbors] + [r for r in rids if r not in neighbors]
    if st.session_state.get(COMPARE_KEY) not in rids or st.session_state.get(COMPARE_KEY) == selected:
        st.session_state[COMPARE_KEY] = default_comparison(group, selected) or selected
    comparison = str(
        st.selectbox(
            "Compare with",
            ordered,
            key=COMPARE_KEY,
            format_func=_member_label,
            help="Second member of the side-by-side comparison.",
        )
    )
    st.caption(
        "Line = direct, stored detection between two members (colour/dash = detection level). "
        + EVIDENCE_NOTE
    )

    if not structure.has_structure:
        info_state(
            "Pair-level structure not stored",
            "This family was rebuilt from a stored payload that never carried its "
            "edge list, so the direct links between its members cannot be drawn "
            "or counted.",
        )
        return selected, comparison

    positions = node_positions(group)
    figure = build_graph_figure(group, labels, positions, selected, neighbors)
    st.plotly_chart(figure, key=GRAPH_KEY, width="stretch")

    with st.expander("Edge list and per-member evidence", expanded=False):
        st.dataframe(
            edges_frame(group, labels),
            key="family_ws_edges",
            width="stretch",
            hide_index=True,
        )
        for label, _rid, n_links, rows in member_evidence(group):
            if not rows:
                st.markdown(f"**{label}** — no direct link recorded")
                continue
            st.markdown(
                f"**{label}** — {n_links} direct link"
                f"{'s' if n_links != 1 else ''} ({n_links} of {len(group.review_ids) - 1} possible)"
            )
            for other, kind, score in rows:
                st.markdown(f"- → **{other}** · {kind_phrase(kind, score)}")
    return selected, comparison


def _render_pair_comparison(group, labels, by_id, structure, selected, comparison) -> None:
    st.markdown("### Side-by-side comparison")
    if not selected or not comparison or selected == comparison:
        st.caption("Select two different members to compare.")
        return
    evidence = {r: (n, rows) for _label, r, n, rows in member_evidence(group)}
    n_links, _rows = evidence.get(selected, (0, [])) if structure.has_structure else (0, [])
    plural = "s" if n_links != 1 else ""
    st.markdown(
        f"**{n_links} direct link{plural}** of {len(group.review_ids) - 1} possible:"
    )
    direct = {
        (min(a, b), max(a, b)): (kind, score)
        for a, b, kind, score in group.edges
    }
    pair = (min(selected, comparison), max(selected, comparison))
    relationship = direct.get(pair)
    if relationship is None:
        st.caption(
            "No direct detector relationship was recorded between these two reviews. "
            + EVIDENCE_NOTE
        )
    else:
        st.caption(
            f"Stored relationship between the two shown reviews: {kind_phrase(*relationship)}."
        )
    left, right = st.columns(2)
    with left:
        _render_member_card(group, labels, by_id, selected)
    with right:
        _render_member_card(group, labels, by_id, comparison)


def _render_member_card(group, labels, by_id, rid: str) -> None:
    review = by_id.get(rid)
    tag = labels.get(rid, "?")
    with st.container(border=True):
        st.markdown(f"**{tag}** · review `{rid}`")
        if review is None:
            st.caption("This member's review record is not present in the place's current reviews.")
            return
        bits = [f"reviewer `{review.reviewer_id}`"]
        if review.rating is not None:
            bits.append(f"{review.rating}★")
        if review.published_at:
            bits.append(f"published {review.published_at[:10]}")
        st.caption(" · ".join(bits))
        render_review_text(review.text_or_empty(), entry_id=rid)


def _render_members(group, by_id: dict) -> None:
    st.markdown("### Family members")
    st.dataframe(
        member_frame(group, by_id),
        key=MEMBERS_TABLE_KEY,
        width="stretch",
        hide_index=True,
    )


def _render_technical_details(group, structure) -> None:
    with st.expander("Technical details", expanded=False):
        st.markdown("**Direct links by detection level**")
        rows = [
            ("Identical text", structure.by_kind["exact"] if structure.has_structure else "not stored"),
            ("Fuzzy pairs", structure.by_kind["fuzzy"] if structure.has_structure else "not stored"),
            ("Near pairs", structure.by_kind["near"] if structure.has_structure else "not stored"),
            ("Semantic pairs", structure.by_kind["semantic"] if structure.has_structure else "not stored"),
        ]
        st.dataframe(
            pd.DataFrame({"Detection level": [r[0] for r in rows], "Direct links": [r[1] for r in rows]}),
            key="family_ws_kinds",
            width="stretch",
            hide_index=True,
        )
        st.markdown("**Relationship matrix (direct links)**")
        matrix = relationship_frame(group)
        if matrix is None:
            st.caption(
                "Pair-level structure not stored for this family — the direct "
                "links between its members cannot be mapped."
            )
        else:
            st.dataframe(matrix, key=MATRIX_KEY, width="stretch")
        st.markdown("**Diagnostics**")
        diagnostics = f"Group id: `{group.group_id}` · average link similarity: {group.avg_similarity:.4f}"
        if structure.has_structure:
            diagnostics += f" · weakest direct link: {structure.weakest:.4f}"
        st.caption(diagnostics)
        if group.signals:
            st.caption("Signals: " + "; ".join(group.signals))
        if group.counter_signals:
            st.caption("Counter-signals: " + "; ".join(group.counter_signals))
