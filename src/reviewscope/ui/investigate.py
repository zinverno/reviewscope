"""Case investigation workspace: open one repeated-text family and stay with it.

Reached from a family card on the Duplicates page or from Discover's place
drill-down, this sub-view presents a single repeated-text family: its members,
the pairs the detector linked *directly*, and — explicitly — where a pair has
no direct link and the reviews meet only through an intermediate member.

Everything derives from an existing ``DuplicateGroup``: membership, ``edges``
and the presentation helpers of :mod:`reviewscope.ui.duplicates`. No score,
label, threshold or grouping rule is added here, and the wording stays neutral
(SPEC.md §29, §36): the view reports detector output and never a verdict.

The selection lives in ``st.session_state`` under this module's keys and is
identified by the family's *member identities* (place id + sorted review ids),
never by a positional index or a run-local group id, so it survives reruns and
page navigation. A dataset switch invalidates it.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine

from .common import info_state
from .duplicates import (
    EVIDENCE_NOTE,
    FAMILY_DEFINITION,
    FAMILY_TERM,
    KIND_LABELS,
    FamilyStructure,
    family_summary_block,
    group_stats,
    member_evidence,
    member_labels,
)

#: Session-state key: identity of the selected family (see :func:`family_identity`).
FAMILY_KEY = "investigation_family"
#: Session-state key: dataset (db path) the selection belongs to.
FAMILY_DB_KEY = "investigation_family_db"
#: Session-state key: workspace-versus-list mode.
OPEN_KEY = "investigation_family_open"

#: Widget keys, exported so tests can address them directly.
SWITCHER_KEY = "investigation_family_switcher"
MEMBER_KEY = "investigation_member"
BACK_KEY = "family_ws_back"
RESUME_KEY = "family_ws_resume"
CLEAR_KEY = "family_ws_clear"
TEXT_KEY = "family_ws_text"
MEMBERS_TABLE_KEY = "family_ws_members"
MATRIX_KEY = "family_ws_matrix"

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


# ---------------------------------------------------------------------------
# Selection state
# ---------------------------------------------------------------------------


def clear_investigation() -> None:
    """Forget the selection entirely: mode, identity, dataset and widget values."""
    for key in (FAMILY_KEY, FAMILY_DB_KEY, OPEN_KEY, SWITCHER_KEY, MEMBER_KEY):
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
    info_state(
        "Investigation in progress",
        f"{FAMILY_TERM} with {len(group.review_ids)} reviews is selected on this place.",
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
    group = lookup[chosen]

    structure = FamilyStructure(group)
    labels = member_labels(group)
    members = [by_id[rid] for rid in group.review_ids if rid in by_id]
    avg_rating, when, concentration = group_stats(members)
    family_summary_block(group, structure, members, avg_rating, when, concentration)
    if group.signals:
        st.markdown("**Signals:** " + "; ".join(group.signals))
    if group.counter_signals:
        st.markdown("**Counter-signals:** " + "; ".join(group.counter_signals))

    _render_members(group, by_id)
    _render_inspector(group, labels, by_id, structure)
    _render_relationships(group, structure)


def _render_members(group, by_id: dict) -> None:
    st.markdown("### Members")
    st.dataframe(
        member_frame(group, by_id),
        key=MEMBERS_TABLE_KEY,
        width="stretch",
        hide_index=True,
    )


def _render_inspector(group, labels: dict, by_id: dict, structure) -> None:
    st.markdown("### Member inspector")
    if st.session_state.get(MEMBER_KEY) not in group.review_ids:
        st.session_state[MEMBER_KEY] = group.review_ids[0]
    rid = str(
        st.selectbox(
            "Member to inspect",
            group.review_ids,
            key=MEMBER_KEY,
            format_func=lambda r: f"{labels.get(r, '?')} · "
            + (by_id[r].text_or_empty()[:60] if r in by_id else r),
            help="Pick a member to read its full text and only its direct links.",
        )
    )
    review = by_id.get(rid)
    st.markdown(f"**{labels.get(rid, '?')}** · review `{rid}`")
    if review is not None:
        bits = [f"reviewer `{review.reviewer_id}`"]
        if review.rating is not None:
            bits.append(f"{review.rating}★")
        if review.published_at:
            bits.append(f"published {review.published_at[:10]}")
        st.caption(" · ".join(bits))
        text = review.text_or_empty()
        if st.session_state.get(TEXT_KEY) != text:
            st.session_state[TEXT_KEY] = text
        default = text if TEXT_KEY not in st.session_state else None
        st.text_area("Review text", default, key=TEXT_KEY, disabled=True, height=220)
    else:
        st.caption("This member's review record is not present in the place's current reviews.")

    if not structure.has_structure:
        st.caption(
            "Pair-level structure was not stored for this family, so direct links "
            "cannot be shown."
        )
        return
    evidence = {r: (n, rows) for _label, r, n, rows in member_evidence(group)}
    n_links, link_rows = evidence.get(rid, (0, []))
    if not link_rows:
        st.markdown("**No direct link recorded** to any other member of this family.")
        return
    plural = "s" if n_links != 1 else ""
    st.markdown(
        f"**{n_links} direct link{plural}** of {len(group.review_ids) - 1} possible:"
    )
    for other, kind, score in link_rows:
        st.markdown(f"- → **{other}** · {kind_phrase(kind, score)}")
    st.caption(EVIDENCE_NOTE)


def _render_relationships(group, structure) -> None:
    st.markdown("### Relationships")
    frame = relationship_frame(group)
    if frame is None:
        info_state(
            "Pair-level structure not stored",
            "This family was rebuilt from a stored payload that never carried its "
            "edge list, so the direct links between its members cannot be shown.",
        )
        return
    st.dataframe(frame, key=MATRIX_KEY, width="stretch")
    st.caption(EVIDENCE_NOTE)
