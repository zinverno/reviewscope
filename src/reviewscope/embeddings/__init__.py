"""Embedding provider abstraction and sentence-transformer implementation (SPEC.md §3, §32).

The provider is an ABC so the backend can be swapped later. The cache wrapper
checks ``embeddings_cache`` before calling the provider; after encoding, newly
computed vectors are written back to the same DuckDB table.
"""

from reviewscope.embeddings.base import EmbeddingProvider
from reviewscope.embeddings.cache import EmbeddingCache
from reviewscope.embeddings.sentence_transformer import SentenceTransformerProvider

__all__ = ["EmbeddingCache", "EmbeddingProvider", "SentenceTransformerProvider"]
