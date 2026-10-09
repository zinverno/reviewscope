"""Repeated-text families page (SPEC.md §29): connected-family semantics.

A ``DuplicateGroup`` is a *connected component* of detected pair relationships,
not a set of mutually similar reviews. The page therefore says "family" rather
than "group", states the connectivity definition up front, and offers pair-level
evidence so a reader can see *why* each review belongs.

Interpretation stays neutral ("repeated review pattern", "high textual
similarity") and never asserts fraud. Rating alignment is shown as separate
context only: it never includes, excludes or re-ranks a member.

Presentation helpers are module-level and pure so tests can assert on the copy
without booting Streamlit.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState, escape_review_text, info_state

_DETECTION_KINDS = ("exact", "fuzzy", "near", "semantic")

#: Product framing for what a DuplicateGroup is.
FAMILY_TERM = "Repeated-text family"

#: Explanatory copy. Stated verbatim wherever the grouping rule is explained.
FAMILY_DEFINITION = (
    "Reviews are grouped when they are connected through one or more strong "
    "text-similarity relationships. Not every pair inside a larger family must "
    "directly pass the similarity threshold."
)

#: Shown when at least one member is only reachable through another member.
TRANSITIVE_LABEL = "Contains transitive connections"

#: Shown when fewer than half of all possible pairs are directly linked.
TRANSITIVE_DETAIL = (
    "This larger family contains reviews joined transitively through "
    "intermediate matches."
)

#: Relationship-evidence caveat: absence of a line is meaningful.
EVIDENCE_NOTE = (
    "Only pairs the detector linked directly are listed. A missing line means "
    "no direct link was detected between those two reviews — they may still be "
    "in the same family through an intermediate member."
)

#: Product wording for each detector link kind; shared with the workspace.
KIND_LABELS = {
    "exact": "identical text",
    "fuzzy": "fuzzy match",
    "near": "near duplicate",
    "semantic": "semantic",
}


# ---------------------------------------------------------------------------
# Derived presentation metrics (pure — no Streamlit, no detector)
# ---------------------------------------------------------------------------


class FamilyStructure:
    """Pair-level structure of one family, derived from detected edges only.

    ``has_structure`` is False for a group rebuilt from a stored payload that
    never carried its edge list; every derived value is then ``None`` rather
    than a misleading zero.
    """

    __slots__ = ("n", "possible", "links", "density", "by_kind", "weakest",
                 "transitive", "sparse", "has_structure")

    def __init__(self, group) -> None:
        self.n = len(group.review_ids)
        self.possible = self.n * (self.n - 1) // 2
        self.has_structure = bool(group.edges)
        if not self.has_structure:
            self.links = None
            self.density = None
            self.by_kind = None
            self.weakest = None
            self.transitive = False
            self.sparse = False
            return
        self.links = len(group.edges)
        self.density = self.links / self.possible if self.possible else 0.0
        self.by_kind = {k: 0 for k in _DETECTION_KINDS}
        for edge in group.edges:
            self.by_kind[edge[2]] += 1
        self.weakest = min((edge[3] for edge in group.edges), default=None)
        # Connectivity, not mutual similarity: a component of 3 with only 2
        # detected links necessarily has one pair joined through the third.
        self.transitive = self.n >= 3 and self.links < self.possible
        # Descriptive predicate, not a score: fewer than half of the possible
        # pairs are directly linked.
        self.sparse = bool(self.transitive and self.links * 2 < self.possible)

    def summary(self) -> str:
        """Stats line: detected-link facts only, never an all-pair claim."""
        if not self.has_structure:
            return f"{self.n} reviews · pair-level structure not stored"
        return (
            f"{self.n} reviews · {self.links} direct links of {self.possible} possible "
            f"pairs ({self.density:.0%})"
        )


def family_label(structure: FamilyStructure) -> str:
    """Evidence-aware display label for one family.

    The label names the detection levels actually present in the stored links,
    so a family whose evidence is only semantic is never titled as if the text
    repeated lexically, and a lexically-only family is never titled as if any
    semantic similarity existed. Falls back to the neutral page term when the
    edge list was not stored (nothing to classify on).
    """
    if not structure.has_structure:
        return FAMILY_TERM
    lexical = (
        structure.by_kind["exact"]
        + structure.by_kind["fuzzy"]
        + structure.by_kind["near"]
    )
    if structure.by_kind["semantic"] == 0:
        return "Repeated-text family"
    if lexical == 0:
        return "Semantic similarity family"
    return "Mixed similarity family"


def interpretation(group, structure: FamilyStructure, concentration: float) -> str:
    """One neutral line describing a family.

    The two wordings that would read as an all-pair claim are only used when
    every possible pair really is a detected link. A family whose direct links
    are all *semantic* (never lexical) says so explicitly, so a similarity line
    can never be mistaken for a claim that the texts read as copies.
    """
    complete = structure.has_structure and structure.links == structure.possible
    semantic_only = (
        structure.has_structure
        and structure.by_kind["exact"] == 0
        and structure.by_kind["fuzzy"] == 0
        and structure.by_kind["near"] == 0
    )
    if group.exact_count >= 2 and group.exact_count >= structure.n * 0.5:
        return "Repeated review pattern — several reviews share identical text."
    if group.exact_count >= 2:
        return "Repeated review pattern — identical text plus close variants."
    if semantic_only:
        if complete:
            return "Strong semantic similarity was detected between directly linked reviews."
        return "Strong semantic similarity was detected along the family's direct links."
    if group.avg_similarity >= 0.9:
        if complete:
            return "High textual similarity — reviews read as near-copies of each other."
        return "High textual similarity along the family's direct links."
    if group.avg_similarity >= 0.8:
        if complete:
            return "High textual similarity across the family."
        return "High textual similarity along the family's direct links."
    if concentration >= 0.9:
        return "Similar reviews concentrated in a short time frame."
    if complete:
        return "Semantically similar reviews grouped together."
    return "Reviews connected through one or more text-similarity relationships."


def member_labels(group) -> dict[str, str]:
    """Stable A, B, C … labels following the family's own member order."""
    labels: dict[str, str] = {}
    for i, rid in enumerate(group.review_ids):
        if i < 26:
            labels[rid] = chr(ord("A") + i)
        else:
            labels[rid] = f"A{chr(ord('A') + i - 26)}"
    return labels


def member_evidence(group) -> list[tuple[str, str, int, list[tuple[str, str, float]]]]:
    """Per-review direct links, as ``(label, review_id, links, rows)``.

    ``rows`` are ``(other_label, kind, score)`` sorted by label. A review with
    fewer rows than ``family_size - 1`` is not directly linked to every other
    member — that is the fact the page exists to show.
    """
    labels = member_labels(group)
    by_review: dict[str, list[tuple[str, str, float]]] = {rid: [] for rid in group.review_ids}
    for a, b, kind, score in group.edges:
        if a in by_review and b in by_review:
            by_review[a].append((labels[b], kind, score))
            by_review[b].append((labels[a], kind, score))
    out = []
    for rid in group.review_ids:
        rows = sorted(by_review[rid], key=lambda r: r[0])
        out.append((labels[rid], rid, len(rows), rows))
    return out


def rating_context(members) -> dict[str, float | int | None]:
    """Rating alignment as *corroborating context* only.

    Deliberately returns plain descriptive values: nothing here is ever used to
    include or exclude a member, to rank families, or to suggest coordination.
    """
    ratings = [m.rating for m in members if m.rating is not None]
    if not ratings:
        return {"available": False, "low": None, "high": None, "spread": None,
                "identical_share": None, "n": 0}
    low, high = min(ratings), max(ratings)
    # Share of reviews holding the most common rating (1.0 only when all match).
    modal = max(ratings.count(r) for r in set(ratings))
    return {
        "available": True,
        "low": low,
        "high": high,
        "spread": high - low,
        "identical_share": modal / len(ratings),
        "n": len(ratings),
    }


def rating_line(context: dict) -> str:
    """Compact rating-context sentence, explicitly separated from text matching."""
    if not context["available"]:
        return "Rating context: no ratings on record."
    spread = context["spread"]
    identical = context["identical_share"]
    return (
        f"Rating context (separate from text matching): {context['low']}–{context['high']}★, "
        f"spread {spread}, {identical:.0%} of these reviews share the same rating. "
        f"Rating alignment never decides who is in a family."
    )


# ---------------------------------------------------------------------------
# Shared presentation: family cards and the case investigation workspace
# ---------------------------------------------------------------------------


def group_stats(members: list) -> tuple[float | None, list[str], float]:
    ratings = [m.rating for m in members if m.rating is not None]
    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    when = sorted({(m.published_at or "")[:10] for m in members if m.published_at})
    distinct_days = len(when)
    n = len(members)
    concentration = 1.0
    if n > 1 and distinct_days > 1:
        concentration = 1.0 - (distinct_days - 1) / (n - 1)
    return avg_rating, when, round(max(0.0, min(1.0, concentration)), 3)


def family_summary_block(
    group,
    structure: FamilyStructure,
    members: list,
    avg_rating: float | None,
    when: list[str],
    concentration: float,
) -> None:
    """Title, stats line, rating context, interpretation and transitive note.

    Shared verbatim by the family cards and the investigation workspace so
    both views state the same facts in the same words. The title names the
    detection evidence actually present (see :func:`family_label`), never an
    unverified category.
    """
    st.markdown(f"**{family_label(structure)}** · {len(members)} reviews")
    stats = [structure.summary()]
    if structure.has_structure:
        stats.append(f"similarity {group.avg_similarity:.2f} (avg over direct links)")
    if avg_rating is not None:
        stats.append(f"avg rating {avg_rating:.2f}")
    stats.append(
        f"date spread {' → '.join([when[0], when[-1]]) if len(when) > 1 else (when[0] if when else 'no dates')}"
    )
    stats.append(f"date concentration {concentration:.0%}")
    st.caption(" · ".join(stats))
    st.caption(rating_line(rating_context(members)))
    st.markdown(interpretation(group, structure, concentration))

    if structure.transitive:
        st.markdown(f"**{TRANSITIVE_LABEL}**")
        if structure.sparse:
            st.caption(TRANSITIVE_DETAIL)
        st.caption(FAMILY_DEFINITION)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def render_duplicates_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    from .investigate import (
        family_identity,
        maybe_render_workspace,
        render_resume_bar,
        set_investigation,
    )

    p = engine.analyze(place_id)
    st.header("Repeated-text families")
    st.caption(
        "Reviews that share identical, near-identical or strongly similar text. "
        "This page reports what the detectors found; it does not assert fraud. "
        + FAMILY_DEFINITION
    )

    groups = [g for g in p.duplicate_groups if len(g.review_ids) >= 2]
    by_id = {r.review_id: r for r in p.reviews}

    if not groups:
        info_state(
            "No repeated-text families",
            "No reviews for this place matched at any detection level.",
            hint="A clean result — most review text here looks unique.",
        )
        return

    db_path = store.db_path or ""
    if maybe_render_workspace(place_id, db_path, groups, by_id):
        return

    members_all = {rid for g in groups for rid in g.review_ids}
    involved = len(members_all & set(by_id))
    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    col1.metric("Repeated-text families", len(groups))
    col2.metric("Reviews involved", involved)
    col3.metric("Share of place reviews", f"{involved / max(len(p.reviews), 1) * 100:.1f}%")

    render_resume_bar(groups, place_id, db_path)

    # --- filters -------------------------------------------------------------
    with st.expander("Filter families", expanded=False):
        largest_group = max(len(g.review_ids) for g in groups)
        if largest_group > 2:
            min_size = st.slider(
                "Minimum family size",
                2,
                largest_group,
                2,
                help="Families with at least this many reviews.",
            )
        else:
            # Streamlit rejects an empty slider range, and a place whose
            # largest repeated-text family has 2 reviews has nothing to filter.
            st.caption(
                "Family-size filter is inactive: this place's largest repeated-text "
                "family has 2 reviews."
            )
            min_size = 2
        kinds = st.multiselect(
            "Detection levels to include",
            _DETECTION_KINDS,
            default=list(_DETECTION_KINDS),
            help="Include families that matched any of these detection levels.",
        )
        min_similarity = st.slider(
            "Minimum average similarity",
            0.0,
            1.0,
            0.0,
            0.05,
            help="Only show families whose average similarity over detected links is at least this value.",
        )
        min_concentration = st.slider(
            "Minimum date concentration",
            0.0,
            1.0,
            0.0,
            0.05,
            help="1.0 = all reviews in the family were published on the same day.",
        )
    sort_by = st.radio(
        "Sort families by",
        ["family size", "average similarity", "date concentration"],
        horizontal=True,
    )

    # --- build the display list ----------------------------------------------
    shown = []
    for g in groups:
        if len(g.review_ids) < min_size:
            continue
        counts = {
            "exact": g.exact_count,
            "fuzzy": g.fuzzy_count,
            "near": g.near_count,
            "semantic": g.semantic_count,
        }
        if not any(counts.get(k, 0) > 0 for k in kinds):
            continue
        members = [by_id[rid] for rid in g.review_ids if rid in by_id]
        if not members:
            continue
        avg_rating, when, concentration = group_stats(members)
        if g.avg_similarity < min_similarity:
            continue
        if concentration < min_concentration:
            continue
        shown.append((g, members, avg_rating, when, concentration))

    if not shown:
        st.info("No repeated-text families match the current filters.")
        return

    if sort_by == "average similarity":
        shown.sort(key=lambda t: t[0].avg_similarity, reverse=True)
    elif sort_by == "date concentration":
        shown.sort(key=lambda t: t[4], reverse=True)
    else:
        shown.sort(key=lambda t: len(t[1]), reverse=True)

    st.caption(f"{len(shown)} famil{'ies' if len(shown) != 1 else 'y'} shown.")

    # --- family cards ---------------------------------------------------------
    for index, (g, members, avg_rating, when, concentration) in enumerate(shown):
        structure = FamilyStructure(g)
        labels = member_labels(g)
        with st.container(border=True):
            family_summary_block(g, structure, members, avg_rating, when, concentration)

            if structure.has_structure:
                with st.expander("Why these reviews are in this family", expanded=False):
                    st.caption(EVIDENCE_NOTE)
                    for label, _rid, n_links, rows in member_evidence(g):
                        if not rows:
                            st.markdown(f"**{label}** — no direct link recorded")
                            continue
                        st.markdown(
                            f"**{label}** — {n_links} direct link"
                            f"{'s' if n_links != 1 else ''} "
                            f"({n_links} of {len(members) - 1} possible)"
                        )
                        for other, kind, score in rows:
                            if kind == "semantic":
                                st.markdown(f"- → **{other}** · semantic similarity {score:.2f}")
                            else:
                                st.markdown(f"- → **{other}** · {KIND_LABELS[kind]}")

            with st.expander("Review texts in this family", expanded=False):
                for m in members:
                    rating = f"{m.rating}★" if m.rating is not None else "no rating"
                    when_m = (m.published_at or "no date")[:10]
                    tag = labels.get(m.review_id, "?")
                    st.markdown(
                        f"- **{tag}** · {rating} · {when_m} · "
                        f"reviewer {escape_review_text(m.reviewer_id)} — "
                        f"{escape_review_text(m.text_or_empty(), limit=160)}"
                    )
                if g.signals:
                    st.markdown("**Signals:** " + "; ".join(g.signals))
                if g.counter_signals:
                    st.markdown("**Counter-signals:** " + "; ".join(g.counter_signals))

            with st.expander("Detection breakdown", expanded=False):
                st.caption(
                    "Identical-text counts are review counts; fuzzy/near/semantic "
                    "counts count detected pairs of reviews."
                )
                rows = [
                    ("Reviews (members)", len(members)),
                    ("Identical-text reviews", g.exact_count),
                    ("Fuzzy pairs", g.fuzzy_count),
                    ("Near-duplicate pairs", g.near_count),
                    ("Semantic pairs", g.semantic_count),
                ]
                if structure.has_structure:
                    rows += [
                        ("Possible pairs", structure.possible),
                        ("Direct links (detected pairs)", structure.links),
                        ("Link density", f"{structure.density:.0%}"),
                        (
                            "Identical-text links",
                            structure.by_kind["exact"],
                        ),
                        ("Average link similarity", f"{g.avg_similarity:.4f}"),
                        (
                            "Weakest direct link",
                            f"{structure.weakest:.4f}" if structure.weakest is not None else "—",
                        ),
                    ]
                else:
                    rows.append(("Pair-level structure", "not stored for this group"))
                st.dataframe(
                    pd.DataFrame({"Measure": [r[0] for r in rows], "Value": [r[1] for r in rows]}),
                    width="stretch",
                )
                if structure.has_structure:
                    st.caption(FAMILY_DEFINITION)

            if st.button(
                "Open investigation workspace",
                key=f"open_family_ws_{index}",
                width="stretch",
            ):
                set_investigation(family_identity(place_id, g.review_ids), db_path)
                st.rerun()
