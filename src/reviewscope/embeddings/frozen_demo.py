"""Model-free replay of the one reviewed, historical demo vector set."""

import hashlib
import json

import numpy as np

from reviewscope.embeddings.base import EmbeddingKey, embedding_key
from reviewscope.storage import DuckDBStore

# The legacy manifest did not record a model revision. Keep these results in
# their own namespace, and opt in only for the PR-04 default configuration.
DEFAULT_REPLAY_CONTRACT = "rs-embedding-v2:e20694aca8495d2edd51573dd3c373264f4d03e10190e22d1760f1358f4ebfd9"
SNAPSHOT = "b04e06b7cae1d945cb5b898ef9a969f4349cec092f9ee6b70ea18fa979e2385b"
FROZEN_ID = "rs-frozen-demo:" + SNAPSHOT


def frozen_demo_vectors(store: DuckDBStore) -> dict[EmbeddingKey, np.ndarray]:
    """Authenticate the exact inputs and float32 outputs on the open connection."""
    if store.review_count() != 1058 or store.cached_embedding_count() != 1058:
        return {}
    rows = store.connection().execute(
        "SELECT review_id, text, text_hash, embedding FROM embeddings_cache "
        "JOIN reviews USING (review_id) WHERE model_name = ? AND len(embedding) = 384 "
        "ORDER BY review_id, text_hash LIMIT 1059",
        ["sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"],
    ).fetchall()
    if len(rows) != 1058:
        return {}
    digest = hashlib.sha256()
    vectors = {}
    for review_id, text, text_hash, embedding in rows:
        header = json.dumps([review_id, text, text_hash], ensure_ascii=False, separators=(",", ":"))
        digest.update(hashlib.sha256(header.encode()).digest())
        vec = np.asarray(embedding, dtype="<f4")
        digest.update(vec.tobytes())
        vectors[embedding_key(review_id, text or "", FROZEN_ID)] = vec
    return vectors if digest.hexdigest() == SNAPSHOT else {}
