"""Choosing the embedding model and its URL: the endpoint embedder
(esamkit.embedders.openai_compat), the factory, the profile keys and the probe.
Runs against a fake /v1/embeddings endpoint (tests/conftest.py)."""

from dataclasses import replace

import pytest

from conftest import dead_url
from esamkit import environment as env
from esamkit.config import default_model_dir, settings
from esamkit.embedders.openai_compat import OpenAICompatEmbedder, identity
from esamkit.embedders.prefixes import QUERY_PREFIX
from esamkit.factory import embedder_identity, get_embedder
from esamkit.llm import EndpointError


def serve_embeddings(endpoint, dim=4, reverse=False):
    def handler(body):
        # each vector's first component encodes the input's length, so order can be checked
        data = [{"index": i, "embedding": [float(len(text))] + [0.0] * (body.get("dimensions", dim) - 1)}
                for i, text in enumerate(body["input"])]
        return 200, {"data": list(reversed(data)) if reverse else data, "model": body["model"]}
    endpoint.handlers["/embeddings"] = handler


def test_model_and_url_are_all_it_needs(endpoint):
    serve_embeddings(endpoint, dim=6)
    emb = OpenAICompatEmbedder(endpoint.url, "sk-e", "nomic-embed-text")
    assert emb.dim == 6 and emb.model_id == "openai-compat:nomic-embed-text"
    assert endpoint.calls[0][0] == "/embeddings" and endpoint.calls[0][1]["model"] == "nomic-embed-text"
    assert endpoint.calls[0][2]["Authorization"] == "Bearer sk-e"
    assert "nomic-embed-text" in emb.describe() and endpoint.url in emb.describe()


def test_it_says_which_of_the_two_is_missing():
    with pytest.raises(ValueError, match="ESAM_EMBED_BASE_URL and ESAM_EMBED_MODEL"):
        OpenAICompatEmbedder("", "", "")
    with pytest.raises(ValueError, match="needs ESAM_EMBED_MODEL:"):
        OpenAICompatEmbedder("http://127.0.0.1:1/v1", "", "")


def test_batches_keep_input_order(endpoint):
    serve_embeddings(endpoint, reverse=True)                     # the server answers out of order
    emb = OpenAICompatEmbedder(endpoint.url, "", "some-model", batch_size=2)
    endpoint.calls.clear()
    vecs = emb.embed_documents(["a", "bb", "ccc", "dddd", "eeeee"])
    assert [v[0] for v in vecs] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [len(c[1]["input"]) for c in endpoint.calls] == [2, 2, 1]
    assert "Authorization" not in endpoint.calls[0][2]            # no key, no header


def test_prefixes_follow_the_model_family_and_can_be_overridden(endpoint):
    serve_embeddings(endpoint)
    bge = OpenAICompatEmbedder(endpoint.url, "", "BAAI/bge-base-en-v1.5")
    endpoint.calls.clear()
    bge.embed_query("brake data lost"), bge.embed_documents(["a chunk"])
    assert endpoint.calls[0][1]["input"] == [QUERY_PREFIX["bge"] + "brake data lost"]
    assert endpoint.calls[1][1]["input"] == ["a chunk"]
    e5 = OpenAICompatEmbedder(endpoint.url, "", "intfloat/e5-base-v2")
    plain = OpenAICompatEmbedder(endpoint.url, "", "BAAI/bge-base-en-v1.5", query_prefix_="none")
    endpoint.calls.clear()
    e5.embed_query("q"), e5.embed_documents(["d"]), plain.embed_query("q")
    assert [c[1]["input"] for c in endpoint.calls] == [["query: q"], ["passage: d"], ["q"]]


def test_dimensions_are_requested_and_part_of_the_identity(endpoint):
    serve_embeddings(endpoint, dim=1536)
    emb = OpenAICompatEmbedder(endpoint.url, "k", "text-embedding-3-small", dimensions=256)
    assert emb.dim == 256 and endpoint.calls[0][1]["dimensions"] == 256
    assert emb.model_id == identity("text-embedding-3-small", 256) == "openai-compat:text-embedding-3-small@256"


def test_a_short_answer_or_a_wrong_model_is_an_error(endpoint):
    endpoint.handlers["/embeddings"] = lambda b: (200, {"data": []})
    with pytest.raises(EndpointError, match="expected 1 embeddings"):
        OpenAICompatEmbedder(endpoint.url, "", "m")
    endpoint.handlers["/embeddings"] = lambda b: (404, {"error": {"message": "model 'm' not found"}})
    with pytest.raises(EndpointError, match="HTTP 404.*not found"):
        OpenAICompatEmbedder(endpoint.url, "", "m")


def test_factory_builds_it_from_settings(endpoint):
    serve_embeddings(endpoint)
    s = replace(settings, embed_backend="openai", embed_base_url=endpoint.url, embed_model="my-model", embed_api_key="")
    emb = get_embedder(s)
    assert emb.model_id == embedder_identity(s) == "openai-compat:my-model"
    assert embedder_identity(replace(settings, embed_backend="local", embed_model_id="BAAI/bge-small-en-v1.5")) == "BAAI/bge-small-en-v1.5"
    assert s.openai_base_url == endpoint.url and s.openai_embed_model == "my-model"      # the older names


def test_default_model_folder_follows_the_model_name():
    assert default_model_dir("BAAI/bge-base-en-v1.5") == "models/bge-base-en-v1.5"
    assert default_model_dir("intfloat/E5-base-v2") == "models/e5-base-v2"


# ── profiles ─────────────────────────────────────────────────────────────────

def test_local_profile_takes_model_and_url_from_the_environment(monkeypatch):
    for v in ("ESAM_EMBED_MODEL", "ESAM_EMBED_BASE_URL", "ESAM_EMBED_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    prof = env.load_profile("local")
    s = env.settings_for(prof)
    assert s.embed_backend == "local" and s.embed_model_id == "BAAI/bge-base-en-v1.5"
    assert s.embed_model_dir.name == "bge-base-en-v1.5"

    monkeypatch.setenv("ESAM_EMBED_MODEL", "BAAI/bge-small-en-v1.5")                    # another local model
    s = env.settings_for(prof)
    assert s.embed_backend == "local" and s.embed_model_id == "BAAI/bge-small-en-v1.5"
    assert s.embed_model_dir.name == "bge-small-en-v1.5"

    monkeypatch.setenv("ESAM_EMBED_MODEL", "nomic-embed-text")                          # a URL: the endpoint
    monkeypatch.setenv("ESAM_EMBED_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("ESAM_EMBED_API_KEY", "sk-e")
    s = env.settings_for(prof)
    assert (s.embed_backend, s.embed_model, s.embed_base_url, s.embed_api_key) == (
        "openai", "nomic-embed-text", "http://127.0.0.1:11434/v1", "sk-e")
    assert embedder_identity(s) == "openai-compat:nomic-embed-text"
    choice = env.embedding_choice(prof)
    assert choice["model_source"] == "env ESAM_EMBED_MODEL" and choice["url_source"] == "env ESAM_EMBED_BASE_URL"


def test_a_profile_can_state_model_and_url_itself():
    prof = {"name": "t", "embeddings": {"backend": "openai", "model": "text-embedding-3-small",
                                         "base_url": "https://gw.example/v1", "dimensions": 512}}
    s = env.settings_for(prof)
    assert (s.embed_backend, s.embed_model, s.embed_base_url, s.embed_dimensions) == (
        "openai", "text-embedding-3-small", "https://gw.example/v1", 512)
    older = {"name": "t", "embeddings": {"backend": "local", "model_id": "BAAI/bge-base-en-v1.5", "model_dir": "models/x"}}
    s = env.settings_for(older)                                   # the keys profiles had before still work
    assert s.embed_backend == "local" and s.embed_model_id == "BAAI/bge-base-en-v1.5" and s.embed_model_dir.name == "x"


def test_pilot_profiles_use_the_staged_local_model_until_the_environment_file_says_otherwise(monkeypatch):
    for v in ("ESAM_EMBED_BASE_URL", "ESAM_OPENAI_BASE_URL", "ESAM_EMBED_BACKEND", "ESAM_EMBED_MODEL", "ESAM_DIAGRAMS"):
        monkeypatch.delenv(v, raising=False)
    for name in ("pilot-il5", "full-janusgraph"):
        s = env.settings_for(env.load_profile(name))
        assert s.embed_backend == "local" and s.embed_base_url == "" and s.diagrams == "local"
        assert s.embed_model_id == "BAAI/bge-base-en-v1.5" and s.embed_model_dir.name == "bge-base-en-v1.5"
    # the profiles name the .env variables, so a gateway embeddings route is one line in .env
    monkeypatch.setenv("ESAM_EMBED_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("ESAM_EMBED_MODEL", "gateway-embed")
    for name in ("pilot-il5", "full-janusgraph"):
        s = env.settings_for(env.load_profile(name))
        assert (s.embed_backend, s.embed_base_url, s.embed_model) == ("openai", "https://gateway.example/v1", "gateway-embed")


def test_probe_checks_the_endpoint_and_reports_model_and_url(endpoint):
    serve_embeddings(endpoint, dim=768)
    prof = {"name": "t", "network": {"internet": False},
            "embeddings": {"backend": "openai", "model": "my-model", "base_url": endpoint.url}}
    s = env.settings_for(prof)
    p = env.probe_embeddings(prof, s)
    assert p.status == "ok" and "my-model" in p.detail and endpoint.url in p.detail and p.data["dim"] == 768
    assert env.probe_embeddings(prof, s, network=False).status == "warning"
    assert endpoint.paths() == ["/embeddings"]                    # --no-network contacted nothing

    down = dict(prof, embeddings=dict(prof["embeddings"], base_url=dead_url()))
    assert env.probe_embeddings(down, env.settings_for(down)).status == "unavailable"
    no_model = dict(prof, embeddings={"backend": "openai", "base_url": endpoint.url})
    p = env.probe_embeddings(no_model, replace(env.settings_for(no_model), embed_model=""))
    assert p.status == "not_configured" and "embeddings.model" in p.detail
    no_url = dict(prof, embeddings={"backend": "openai", "model": "m", "base_url_env": "T_EMBED_URL"})
    p = env.probe_embeddings(no_url, replace(env.settings_for(no_url), embed_base_url=""))
    assert p.status == "not_configured" and "T_EMBED_URL" in p.detail
    public = dict(prof, embeddings={"backend": "openai", "model": "m", "base_url": "https://api.openai.com/v1"})
    assert "air-gapped" in env.probe_embeddings(public, env.settings_for(public)).detail
