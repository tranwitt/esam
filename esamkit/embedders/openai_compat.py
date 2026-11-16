"""
Endpoint embedder: any server that speaks the OpenAI embeddings protocol
(POST <base_url>/embeddings). That covers self-hosted servers (vLLM, Hugging
Face TEI, Ollama, LiteLLM), an API gateway, and OpenAI itself.

You choose two things, the MODEL and the URL:

    ESAM_EMBED_MODEL=<the model name the endpoint serves>
    ESAM_EMBED_BASE_URL=https://<host>/v1          # ESAM posts to <this>/embeddings
    ESAM_EMBED_API_KEY=...                         # if the endpoint wants one

Setting the URL is enough to switch from the local model to the endpoint
(ESAM_EMBED_BACKEND=openai is implied). In a profile the same three are
`embeddings.model`, `embeddings.base_url` (or `base_url_env`) and
`embeddings.api_key_env`; see config/environments/local.yaml. Check what is
in effect, and that the endpoint answers, with:

    python -m esamkit.environment

Examples:

    # Ollama on this machine
    ESAM_EMBED_BASE_URL=http://127.0.0.1:11434/v1
    ESAM_EMBED_MODEL=nomic-embed-text

    # A TEI or vLLM server hosting the same BGE model as the local backend
    ESAM_EMBED_BASE_URL=http://embeddings.internal:8080/v1
    ESAM_EMBED_MODEL=BAAI/bge-base-en-v1.5

    # OpenAI, with shorter vectors
    ESAM_EMBED_BASE_URL=https://api.openai.com/v1
    ESAM_EMBED_MODEL=text-embedding-3-small
    ESAM_EMBED_DIMENSIONS=768

Queries and stored text may need different prefixes (see base.py). Servers
do not add them, so this client does, by model family: BGE gets its query
instruction, E5 gets "query: " / "passage: ", anything else is sent as it
is. Override with ESAM_EMBED_QUERY_PREFIX / ESAM_EMBED_DOC_PREFIX ("none" =
send the text unchanged).

REMEMBER: switching embedders means re-embedding EVERYTHING. The vector store
will refuse to mix vectors from a different model: this embedder's identity
is "openai-compat:<model>" (plus "@<dimensions>" when you shorten vectors).
"""

import time

from ..llm import EndpointError, post_json
from .base import Embedder
from .prefixes import doc_prefix, query_prefix


RETRIES, RETRY_WAIT = 2, 0.5        # like the OpenAI SDK this client replaced: two more tries


def identity(model: str, dimensions: int = 0) -> str:
    """The model id stored with a collection embedded through an endpoint."""
    return f"openai-compat:{model}" + (f"@{dimensions}" if dimensions else "")


class OpenAICompatEmbedder(Embedder):
    def __init__(self, base_url: str, api_key: str, model: str, dimensions: int = 0, batch_size: int = 64,
                 query_prefix_: str = "auto", doc_prefix_: str = "auto", timeout: float = 120.0):
        missing = [name for name, value in (("ESAM_EMBED_BASE_URL", base_url), ("ESAM_EMBED_MODEL", model)) if not value]
        if missing:
            raise ValueError(
                f"The endpoint embedder needs {' and '.join(missing)}: the URL of an OpenAI-compatible "
                "endpoint (https://<host>/v1) and the name of the embedding model it serves. "
                "In a profile: embeddings.base_url (or base_url_env) and embeddings.model."
            )
        self._url = base_url.rstrip("/") + "/embeddings"
        self._key, self._model, self._timeout = api_key, model, timeout
        self._dimensions, self._batch = dimensions, max(1, batch_size)
        self._query_prefix = query_prefix(model, query_prefix_)
        self._doc_prefix = doc_prefix(model, doc_prefix_)
        self.base_url = base_url.rstrip("/")
        self.model_id = identity(model, dimensions)
        # Ask the server once for a vector so we learn the dimension instead
        # of hard-coding it (the old script hard-coded 768).
        self.dim = len(self._embed(["dimension probe"])[0])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            batch = texts[i:i + self._batch]
            payload: dict = {"model": self._model, "input": batch}
            if self._dimensions:
                payload["dimensions"] = self._dimensions
            data = self._post(payload).get("data")
            if not isinstance(data, list) or len(data) != len(batch):
                raise EndpointError(self._url, None, f"expected {len(batch)} embeddings for model {self._model!r}, "
                                                     f"got {len(data) if isinstance(data, list) else 'none'}")
            # Answers carry an index; sort so output order matches input order.
            out += [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]
        return out

    def _post(self, payload: dict) -> dict:
        """One request, tried again (twice, briefly) when the failure is the
        passing kind: no connection, a timeout, 408/409/429 or a 5xx. A
        rejected key or an unknown model fails at once, saying what to check."""
        for attempt in range(RETRIES + 1):
            try:
                return post_json(self._url, payload, self._key, self._timeout)
            except EndpointError as e:
                if e.status in (401, 403):
                    raise EndpointError(self._url, e.status, f"{e.detail} [the endpoint refused the request: " + (
                        "check ESAM_EMBED_API_KEY" if self._key else
                        "no API key was sent; set ESAM_EMBED_API_KEY") + "]") from None
                if attempt == RETRIES or not (e.status is None or e.status in (408, 409, 429) or e.status >= 500):
                    raise
                time.sleep(RETRY_WAIT * (attempt + 1))
        raise AssertionError("unreachable")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self._doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_prefix + text])[0]

    def describe(self) -> str:
        return f"{type(self).__name__}(model={self._model!r}, url={self._url!r}, dim={self.dim})"
