"""Dataset-level discovery layer (Phase 16).

Surfaces the analysis ReviewScope already produces for a *single* place across
**every** place of the selected dataset, so a user with hundreds of places can
answer "which organizations are worth investigating?".

Design rules (kept deliberately strict):

* every number comes from the production
  :class:`~reviewscope.analysis.engine.AnalysisEngine` or from the production
  scorers it already calls — no detector, threshold, weight or formula is
  duplicated or re-derived here;
* nothing is labelled *fake*, *fraudulent* or *manipulated*: the output is a
  descriptive ranking of observed evidence;
* evidence the dataset cannot provide is ``None`` (rendered as ``N/A``), never
  ``0`` — missing evidence is not negative evidence;
* the expensive per-place pass runs once per dataset identity and is cached in
  process and on disk.
"""

from reviewscope.discovery.summary import (
    CACHE_VERSION,
    DISPLAY_COLUMNS,
    DUP_RATE_MIN_GROUP,
    NOT_AVAILABLE,
    PLACE_COLUMNS,
    SORT_OPTIONS,
    DatasetCapabilities,
    DatasetSummary,
    RankingSection,
    build_dataset_summary,
    category_summary,
    clear_dataset_cache,
    dataset_cache_info,
    dataset_identity,
    display_category_frame,
    display_places_frame,
    filter_places,
    get_dataset_summary,
    ranking_sections,
    source_badge,
)

__all__ = [
    "CACHE_VERSION",
    "DISPLAY_COLUMNS",
    "DUP_RATE_MIN_GROUP",
    "NOT_AVAILABLE",
    "PLACE_COLUMNS",
    "SORT_OPTIONS",
    "DatasetCapabilities",
    "DatasetSummary",
    "RankingSection",
    "build_dataset_summary",
    "category_summary",
    "clear_dataset_cache",
    "dataset_cache_info",
    "dataset_identity",
    "display_category_frame",
    "display_places_frame",
    "filter_places",
    "get_dataset_summary",
    "ranking_sections",
    "source_badge",
]
