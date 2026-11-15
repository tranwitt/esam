"""
Local embedder — sentence-transformers running on CPU, loaded from disk.

Air-gap rules this file enforces:
  * The model is loaded from a LOCAL FOLDER (settings.embed_model_dir), never
    by Hugging Face id. Passing an id like "BAAI/bge-base-en-v1.5" to
    SentenceTransformer() makes it try to download — which hangs or fails
    with no internet.
  * HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE are forced on before the library is
    imported, so even an accidental download attempt fails fast and loudly.

Stage the folder once on a connected machine with
    python lessons/00_setup/stage_model.py
then copy esam/models/ across the air gap.
"""

import os
from pathlib import Path

from .base import Embedder
from .prefixes import DOC_PREFIX, QUERY_PREFIX, family

# Query / passage prefixes per model family: see prefixes.py.
_QUERY_PREFIX, _DOC_PREFIX, _family = QUERY_PREFIX, DOC_PREFIX, family


class LocalSentenceTransformerEmbedder(Embedder):
    def __init__(self, model_dir: Path, model_id: str):
        if not Path(model_dir, "config.json").exists():
            raise FileNotFoundError(
                f"No model found in {model_dir} (ESAM_EMBED_MODEL={model_id}).\n"
                "Stage it first (on a machine with internet):\n"
                "    python lessons/00_setup/stage_model.py\n"
                "then copy the esam/models/ folder to this machine.\n"
                "To use another model set ESAM_EMBED_MODEL (and ESAM_EMBED_MODEL_DIR if it is not\n"
                "in models/<name>); to use an embeddings endpoint instead set ESAM_EMBED_BASE_URL."
            )

        # Must be set BEFORE importing sentence_transformers / transformers.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer

        self.model_id = model_id
        self._family = _family(model_id)
        # device="cpu": the ESAM pilot VMs have no GPU, so we learn on the
        # same footing.
        self._model = SentenceTransformer(str(model_dir), device="cpu")
        # Renamed in sentence-transformers 5.x; support both so older wheels
        # on the air-gapped side still work.
        get_dim = (getattr(self._model, "get_embedding_dimension", None)
                   or self._model.get_sentence_embedding_dimension)
        self.dim = get_dim()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prefix = _DOC_PREFIX.get(self._family, "")
        vecs = self._model.encode(
            [prefix + t for t in texts],
            batch_size=16,
            # normalize -> every vector has length 1.0, so cosine similarity
            # becomes a plain dot product. BGE's model card recommends it.
            normalize_embeddings=True,
            show_progress_bar=len(texts) > 32,
        )
        return vecs.tolist()

    def embed_query(self, text: str) -> list[float]:
        prefix = _QUERY_PREFIX.get(self._family, "")
        vec = self._model.encode([prefix + text], normalize_embeddings=True)
        return vec[0].tolist()

    # Lesson 02 uses this to show what a wrong prefix does. Not for normal use.
    def embed_raw(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True).tolist()
