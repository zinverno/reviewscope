import numpy as np

from reviewscope.analysis.duplicates import DuplicateDetector
from reviewscope.config import CONFIG
from reviewscope.models.review import NormalizedReview


def test_exact_precedence_with_floating_point_semantic_similarity():
    r1 = NormalizedReview(
        review_id="a1",
        place_id="p",
        place_name="P",
        place_category="c",
        reviewer_id="u1",
        reviewer_name="U",
        rating=5,
        text="same text here",
        published_at="2026-01-01T00:00:00",
        city="c",
        region="r",
        country="C",
        latitude=0.0,
        longitude=0.0,
        source="d",
    )
    r2 = NormalizedReview(
        review_id="a2",
        place_id="p",
        place_name="P",
        place_category="c",
        reviewer_id="u2",
        reviewer_name="U",
        rating=5,
        text="same text here",
        published_at="2026-01-02T00:00:00",
        city="c",
        region="r",
        country="C",
        latitude=0.0,
        longitude=0.0,
        source="d",
    )
    emb = np.array([[1.0, 0.0], [1.0000002, 0.0]], dtype=float)
    det = DuplicateDetector(CONFIG.duplicate)
    groups = det.detect([r1, r2], embeddings=emb)
    assert groups
    g = groups[0]
    assert g.edges
    assert g.edges[0][2] == "exact"
    assert g.edges[0][3] == 1.0
