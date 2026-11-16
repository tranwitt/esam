"""
Factory — the ONLY place that knows which concrete backend is in use.

Lesson scripts (and later, CrewAI tools) do:

    from esamkit.factory import get_embedder, get_vector_store
    emb = get_embedder()
    store = get_vector_store(emb)

and never import psycopg or sentence_transformers themselves. Moving from
the local Docker database to the pilot's VM-1 is just:

    ESAM_PG_DSN=postgresql://esam_user:***@<vm-1>:5432/esam_pilot

The embedding model is chosen the same way, by MODEL and URL:

    ESAM_EMBED_MODEL=<model>                 # which model
    ESAM_EMBED_BASE_URL=https://<host>/v1    # where it runs; unset = locally, from disk

Adding another vector database later = one new class implementing
VectorStore (vectorstores/base.py) + one more branch below. Backend modules
are imported lazily inside each branch, so you only install what you use.
"""

from .config import Settings, settings as default_settings
from .embedders.base import Embedder
from .graphstores.base import GraphStore
from .vectorstores.base import VectorStore


def get_embedder(s: Settings = default_settings) -> Embedder:
    if s.embed_backend == "local":
        from .embedders.local_st import LocalSentenceTransformerEmbedder
        return LocalSentenceTransformerEmbedder(s.embed_model_dir, s.embed_model_id)
    if s.embed_backend == "openai":
        from .embedders.openai_compat import OpenAICompatEmbedder
        key, source = embed_api_key(s)
        return OpenAICompatEmbedder(s.embed_base_url, key, s.embed_model, s.embed_dimensions,
                                    s.embed_batch_size, s.embed_query_prefix, s.embed_doc_prefix, s.llm_timeout,
                                    key_source=source, client=s.embed_client)
    raise ValueError(f"Unknown ESAM_EMBED_BACKEND={s.embed_backend!r} (use 'local' or 'openai')")


def embed_api_key(s: Settings = default_settings) -> tuple[str, str]:
    """(the key sent to the embeddings endpoint, where it came from).

    ESAM_EMBED_API_KEY (older name: ESAM_OPENAI_API_KEY) when it is set.
    When it is not, and the embeddings endpoint is the SAME host as the
    reasoning model's (one gateway serving both), the reasoning key: that
    host is already given it on every chat call. A different host is never
    sent another endpoint's key."""
    from urllib.parse import urlparse

    from .config import env_source

    if s.embed_api_key:
        return s.embed_api_key, env_source("ESAM_EMBED_API_KEY") or "ESAM_EMBED_API_KEY"
    host = lambda u: urlparse(u or "").netloc.lower()                 # noqa: E731
    if s.chat_api_key and host(s.embed_base_url) and host(s.embed_base_url) == host(s.chat_base_url):
        return s.chat_api_key, (env_source("ESAM_REASONING_API_KEY") or "the reasoning key") + " (same host as the reasoning model)"
    return "", ""


def embedder_identity(s: Settings = default_settings) -> str:
    """The model id a collection embedded with these settings carries,
    without loading or contacting the model."""
    if s.embed_backend == "openai":
        from .embedders.openai_compat import identity
        return identity(s.embed_model, s.embed_dimensions)
    return s.embed_model_id


def get_vector_store(embedder: Embedder, s: Settings = default_settings,
                     collection: str | None = None) -> VectorStore:
    name = collection or s.collection
    if s.vector_backend == "pgvector":
        from .vectorstores.pgvector_store import PgVectorStore
        return PgVectorStore(s.pg_dsn, name, embedder.model_id, embedder.dim)
    raise ValueError(f"Unknown ESAM_VECTOR_BACKEND={s.vector_backend!r} (supported: 'pgvector')")


def get_graph_store(s: Settings = default_settings) -> GraphStore:
    # Note: no embedder needed. The graph holds structure, not vectors.
    if s.graph_backend == "postgres":
        from .graphstores.postgres_cte import PostgresCTEGraphStore
        return PostgresCTEGraphStore(s.graph_dsn or s.pg_dsn)
    raise ValueError(f"Unknown ESAM_GRAPH_BACKEND={s.graph_backend!r} (supported: 'postgres')")
