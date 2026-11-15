"""
Configuration — every knob comes from an environment variable with a safe
local default. Nothing here imports a backend library, so reading config is
always cheap and never triggers a network call or a model load.

Copy `.env.example` to `.env` and edit, or export the variables in your shell.
"""

import os
from dataclasses import dataclass
from pathlib import Path

# Root of the esam/ project (this file lives in esam/esamkit/config.py).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, '#' comments. Existing env wins.

    We avoid the python-dotenv dependency on purpose: one less wheel to carry
    across the air gap.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(PROJECT_ROOT / ".env")


def _path(env_name: str, default: str) -> Path:
    """Read a path from env; relative paths are taken relative to esam/."""
    p = Path(os.getenv(env_name, default))
    return p if p.is_absolute() else PROJECT_ROOT / p


def _first(*names: str, default: str = "") -> str:
    """The first of these environment variables that is set and not empty."""
    for name in names:
        if os.getenv(name):
            return os.environ[name]
    return default


# Older names of variables, still read when the current name is not set.
#   ESAM_CHAT_*              -> ESAM_REASONING_*   (the reasoning model; ESAM_CHAT_* was its first name)
#   ESAM_GATEWAY_URL / _KEY  -> the reasoning AND the coding endpoint (they used to share one gateway setting)
_OLD_PREFIXES = (("ESAM_REASONING_", "ESAM_CHAT_"),)
_OLD_NAMES = {"ESAM_REASONING_BASE_URL": ("ESAM_GATEWAY_URL",), "ESAM_REASONING_API_KEY": ("ESAM_GATEWAY_KEY",),
              "ESAM_CODING_BASE_URL": ("ESAM_GATEWAY_URL",), "ESAM_CODING_API_KEY": ("ESAM_GATEWAY_KEY",)}


def env_names(name: str) -> list[str]:
    """The variable and the older names it is also read from, in order."""
    names = [name] + [old + name[len(new):] for new, old in _OLD_PREFIXES if name.startswith(new)]
    return names + list(_OLD_NAMES.get(name, ()))


def env(name: str | None, default: str = "") -> str:
    """An environment variable (or .env line), by its current or an older name."""
    return _first(*env_names(name), default=default) if name else default


def env_source(name: str | None) -> str:
    """Which variable actually supplied the value ('' when none is set)."""
    return next((n for n in env_names(name) if os.getenv(n)), "") if name else ""


def _fallbacks(prefix: str) -> tuple[tuple[tuple[str, str], ...], ...]:
    """<prefix>_FALLBACK_<n>_BASE_URL / _API_KEY / _MODEL / _API for n = 1, 2, ...
    in order. A fallback states only what differs from the primary endpoint,
    so one variable is enough (e.g. only _API=responses, or only _BASE_URL).
    Numbering stops at the first n with nothing set."""
    out = []
    for n in range(1, 10):
        spec = {field: env(f"{prefix}_FALLBACK_{n}_{var}")
                for field, var in (("base_url", "BASE_URL"), ("api_key", "API_KEY"), ("model", "MODEL"), ("api", "API"))}
        if not any(spec.values()):
            break
        out.append(tuple(sorted((k, v) for k, v in spec.items() if v)))
    return tuple(out)


DEFAULT_EMBED_MODEL = "BAAI/bge-base-en-v1.5"


def default_model_dir(model_id: str) -> str:
    """models/<last part of the model id>, e.g. models/bge-base-en-v1.5."""
    return f"models/{model_id.rstrip('/').split('/')[-1].lower()}"


@dataclass(frozen=True)
class Settings:
    # ── Embeddings ──────────────────────────────────────────────────────────
    # Two things choose the embedding model, wherever it runs:
    #
    #   ESAM_EMBED_MODEL      WHICH model. For the local backend, its Hugging
    #                         Face id; for an endpoint, the name it serves.
    #   ESAM_EMBED_BASE_URL   WHERE it runs: the URL of an OpenAI-compatible
    #                         endpoint (…/v1; ESAM posts to …/v1/embeddings).
    #                         Unset = on this machine, from a folder on disk.
    #
    # "local"  = sentence-transformers model loaded from a folder on disk
    # "openai" = any OpenAI-compatible /v1/embeddings endpoint (vLLM, TEI,
    #            Ollama, a gateway, or OpenAI)
    # The backend follows from the URL: set ESAM_EMBED_BASE_URL and it is
    # "openai", leave it unset and it is "local". ESAM_EMBED_BACKEND overrides.
    embed_backend: str = os.getenv("ESAM_EMBED_BACKEND") or (
        "openai" if _first("ESAM_EMBED_BASE_URL", "ESAM_OPENAI_BASE_URL") else "local")

    # The model's identity. Used (a) by lessons/00_setup/stage_model.py to
    # download a local model ONCE on an internet-connected machine, and (b)
    # stored next to every vector so stores never mix models.
    # (ESAM_EMBED_MODEL_ID is the older name of ESAM_EMBED_MODEL.)
    embed_model_id: str = _first("ESAM_EMBED_MODEL", "ESAM_EMBED_MODEL_ID", default=DEFAULT_EMBED_MODEL)

    # Local backend: the folder that holds the pre-staged model files. At
    # runtime we load ONLY from here, never from the internet. Defaults to
    # models/<last part of the model id>.
    embed_model_dir: Path = _path("ESAM_EMBED_MODEL_DIR", default_model_dir(
        _first("ESAM_EMBED_MODEL", "ESAM_EMBED_MODEL_ID", default=DEFAULT_EMBED_MODEL)))

    # Endpoint backend ("openai"). The ESAM_OPENAI_* names are the older ones
    # and still work.
    embed_base_url: str = _first("ESAM_EMBED_BASE_URL", "ESAM_OPENAI_BASE_URL")
    embed_api_key: str = _first("ESAM_EMBED_API_KEY", "ESAM_OPENAI_API_KEY")
    # The model name sent to the endpoint. Empty = not chosen: the endpoint
    # backend refuses to start rather than guess.
    embed_model: str = _first("ESAM_EMBED_MODEL", "ESAM_OPENAI_EMBED_MODEL")
    # Ask the endpoint for shorter vectors (models that support `dimensions`,
    # e.g. text-embedding-3-*). 0 = the model's own size.
    embed_dimensions: int = int(os.getenv("ESAM_EMBED_DIMENSIONS", "0") or 0)
    embed_batch_size: int = int(os.getenv("ESAM_EMBED_BATCH_SIZE", "64") or 64)
    # Text put in front of every query / stored chunk before it is sent to the
    # endpoint. "auto" = what the model family was trained with (BGE: an
    # instruction on queries; E5: "query: " / "passage: "; others: nothing).
    # "none" = send the text as it is (the server adds the prefix itself).
    embed_query_prefix: str = os.getenv("ESAM_EMBED_QUERY_PREFIX", "auto")
    embed_doc_prefix: str = os.getenv("ESAM_EMBED_DOC_PREFIX", "auto")

    # ── Vector store ────────────────────────────────────────────────────────
    # "pgvector" = PostgreSQL + pgvector: the local Docker container here
    # (docker-compose.yml), VM-1 on the pilot. Other backends can be added
    # behind the VectorStore interface; see factory.py.
    vector_backend: str = os.getenv("ESAM_VECTOR_BACKEND", "pgvector")
    collection: str = os.getenv("ESAM_COLLECTION", "esam_documents")

    # libpq connection string. Default = the local docker-compose database.
    # On the pilot, point this at VM-1 (and keep the password in .env, not here).
    pg_dsn: str = os.getenv(
        "ESAM_PG_DSN", "postgresql://esam_user:esam_local_only@127.0.0.1:5433/esam_pilot")

    # ── Graph store ─────────────────────────────────────────────────────────
    # "postgres" = graph_vertices/graph_edges + recursive CTEs (pilot design).
    # A JanusGraph/Gremlin backend would be a new GraphStore class + a new
    # value here (see lessons/04_graph/gremlin_equivalents.md).
    graph_backend: str = os.getenv("ESAM_GRAPH_BACKEND", "postgres")
    # Defaults to the same database as the vectors, as on the pilot.
    graph_dsn: str = os.getenv("ESAM_GRAPH_DSN", "")

    # ── Models: one endpoint per role ───────────────────────────────────────
    # Each role has its OWN url, key, model, API and fallbacks:
    #   ESAM_REASONING_*   the model that reads, answers and reviews (lessons
    #                      05/06 and all the crew's agents)
    #   ESAM_CODING_*      the model approved for writing code
    #   ESAM_VISION_*      the model that describes picture diagrams
    # The environment profiles (config/environments/*.yaml) name these same
    # variables, so .env is the one place to set them.
    #
    # Reasoning. Any OpenAI-compatible endpoint, e.g. the IL5 API Gateway. If
    # unset, lesson 05 prints the prompt it WOULD send instead of calling a
    # model. (The fields are called chat_* and the variables were ESAM_CHAT_*
    # first; the old names are still read.)
    chat_base_url: str = env("ESAM_REASONING_BASE_URL")
    chat_api_key: str = env("ESAM_REASONING_API_KEY")
    chat_model: str = env("ESAM_REASONING_MODEL")
    # Which API the endpoint speaks: "chat" (/chat/completions, the default),
    # "responses" (/responses) or "auto" (chat, then responses if chat fails).
    chat_api: str = env("ESAM_REASONING_API", "chat")
    # Endpoints tried, in order, when the one above fails (esamkit/llm.py):
    # ESAM_REASONING_FALLBACK_1_BASE_URL, _API_KEY, _MODEL, _API; then _2_, ...
    chat_fallbacks: tuple = _fallbacks("ESAM_REASONING")
    # Coding. Nothing is shared with reasoning: no model or no URL here means
    # there is no coding model.
    coding_base_url: str = env("ESAM_CODING_BASE_URL")
    coding_api_key: str = env("ESAM_CODING_API_KEY")
    coding_model: str = env("ESAM_CODING_MODEL")
    coding_api: str = env("ESAM_CODING_API", "chat")
    coding_fallbacks: tuple = _fallbacks("ESAM_CODING")
    # Seconds to wait for one answer, and how long a failed endpoint is
    # skipped before it is tried again.
    llm_timeout: float = float(os.getenv("ESAM_LLM_TIMEOUT", "120") or 120)
    llm_failover_cooldown: float = float(os.getenv("ESAM_LLM_FAILOVER_COOLDOWN", "300") or 300)

    # ── Picture diagrams (esamkit/diagrams.py) ──────────────────────────────
    # A diagram in a document (a picture file, or a picture inside a Word or
    # markdown document) is turned into text so it can be searched:
    #   auto   a vision model describes it if one is configured below;
    #          otherwise the text found in the picture is used (SVG labels,
    #          OCR when tesseract is installed)
    #   local  never send a picture to a model: local text extraction only
    #   off    keep only the figure's name and caption
    diagrams: str = os.getenv("ESAM_DIAGRAMS", "auto")
    # The model that looks at pictures. No model = no picture leaves this
    # machine. URL, key and API default to the chat model's.
    vision_model: str = os.getenv("ESAM_VISION_MODEL", "")
    vision_base_url: str = os.getenv("ESAM_VISION_BASE_URL", "")
    vision_api_key: str = os.getenv("ESAM_VISION_API_KEY", "")
    vision_api: str = os.getenv("ESAM_VISION_API", "")
    vision_fallbacks: tuple = _fallbacks("ESAM_VISION")
    # Descriptions are kept by picture content + model, so the same diagram is
    # described once however often its document is read.
    diagram_cache_dir: Path = _path("ESAM_DIAGRAM_CACHE_DIR", "work/cache/diagrams")

    # ── Data ────────────────────────────────────────────────────────────────
    baseline_yaml: Path = _path("ESAM_BASELINE_YAML", "data/baseline/vehicle_decomposition.yaml")
    documents_dir: Path = _path("ESAM_DOCUMENTS_DIR", "data/documents")
    # The SE-owned vocabulary the baseline is checked against (esamkit/ontology.py).
    ontology_yaml: Path = _path("ESAM_ONTOLOGY_YAML", "data/ontology/esam_ontology.yaml")

    # ── Word documents (esamkit/readers.py) ─────────────────────────────────
    # How a legacy .doc is read:
    #   auto         LibreOffice if it is installed (full conversion: automatic
    #                numbers, vector pictures), otherwise the built-in reader
    #   libreoffice  LibreOffice only; fail if it is missing
    #   builtin      the built-in reader only (no external program is started)
    # .docx is always read by the built-in reader.
    doc_converter: str = os.getenv("ESAM_DOC_CONVERTER", "auto")
    # Path of the soffice program, if it is not on PATH.
    soffice_path: str = os.getenv("ESAM_SOFFICE", "")

    # ── Chunking ────────────────────────────────────────────────────────────
    chunk_size: int = int(os.getenv("ESAM_CHUNK_SIZE", "600"))       # characters
    chunk_overlap: int = int(os.getenv("ESAM_CHUNK_OVERLAP", "80"))  # characters

    # Older names of the endpoint-embeddings settings.
    @property
    def openai_base_url(self) -> str:
        return self.embed_base_url

    @property
    def openai_api_key(self) -> str:
        return self.embed_api_key

    @property
    def openai_embed_model(self) -> str:
        return self.embed_model


settings = Settings()
