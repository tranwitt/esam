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

try:                                       # the real package, kept before the fixtures hide it
    import openai as REAL_OPENAI
except ImportError:
    REAL_OPENAI = None


@pytest.fixture(autouse=True)
def built_in_client(monkeypatch):
    """These tests describe the built-in HTTP client, so hide the `openai`
    package: with it installed (requirements.txt installs it) client="auto"
    would pick the SDK. Tests that use the `sdk` fixture put a fake one back."""
    import sys
    monkeypatch.setitem(sys.modules, "openai", None)


def serve_embeddings(endpoint, dim=4, reverse=False):
    def handler(body):
        # each vector's first component encodes the input's length, so order can be checked
        data = [{"index": i, "embedding": [float(len(text))] + [0.0] * (body.get("dimensions", dim) - 1)}
                for i, text in enumerate(body["input"])]
        return 200, {"data": list(reversed(data)) if reverse else data, "model": body["model"]}
    endpoint.handlers["/embeddings"] = handler


def test_model_and_url_are_all_it_needs(endpoint):
    serve_embeddings(endpoint, dim=6)
    emb = OpenAICompatEmbedder((endpoint.url + "/embeddings"), "sk-e", "nomic-embed-text")
    assert emb.dim == 6 and emb.model_id == "openai-compat:nomic-embed-text"
    assert endpoint.calls[0][0] == "/embeddings" and endpoint.calls[0][1]["model"] == "nomic-embed-text"
    assert endpoint.calls[0][2]["Authorization"] == "Bearer sk-e"
    assert "nomic-embed-text" in emb.describe() and (endpoint.url + "/embeddings") in emb.describe()


def test_it_says_which_of_the_two_is_missing():
    with pytest.raises(ValueError, match="ESAM_EMBED_BASE_URL and ESAM_EMBED_MODEL"):
        OpenAICompatEmbedder("", "", "")
    with pytest.raises(ValueError, match="needs ESAM_EMBED_MODEL:"):
        OpenAICompatEmbedder("http://127.0.0.1:1/v1", "", "")


def test_batches_keep_input_order(endpoint):
    serve_embeddings(endpoint, reverse=True)                     # the server answers out of order
    emb = OpenAICompatEmbedder((endpoint.url + "/embeddings"), "", "some-model", batch_size=2)
    endpoint.calls.clear()
    vecs = emb.embed_documents(["a", "bb", "ccc", "dddd", "eeeee"])
    assert [v[0] for v in vecs] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [len(c[1]["input"]) for c in endpoint.calls] == [2, 2, 1]
    assert "Authorization" not in endpoint.calls[0][2]            # no key, no header


def test_prefixes_follow_the_model_family_and_can_be_overridden(endpoint):
    serve_embeddings(endpoint)
    bge = OpenAICompatEmbedder((endpoint.url + "/embeddings"), "", "BAAI/bge-base-en-v1.5")
    endpoint.calls.clear()
    bge.embed_query("brake data lost"), bge.embed_documents(["a chunk"])
    assert endpoint.calls[0][1]["input"] == [QUERY_PREFIX["bge"] + "brake data lost"]
    assert endpoint.calls[1][1]["input"] == ["a chunk"]
    e5 = OpenAICompatEmbedder((endpoint.url + "/embeddings"), "", "intfloat/e5-base-v2")
    plain = OpenAICompatEmbedder((endpoint.url + "/embeddings"), "", "BAAI/bge-base-en-v1.5", query_prefix_="none")
    endpoint.calls.clear()
    e5.embed_query("q"), e5.embed_documents(["d"]), plain.embed_query("q")
    assert [c[1]["input"] for c in endpoint.calls] == [["query: q"], ["passage: d"], ["q"]]


def test_dimensions_are_requested_and_part_of_the_identity(endpoint):
    serve_embeddings(endpoint, dim=1536)
    emb = OpenAICompatEmbedder((endpoint.url + "/embeddings"), "k", "text-embedding-3-small", dimensions=256)
    assert emb.dim == 256 and endpoint.calls[0][1]["dimensions"] == 256
    assert emb.model_id == identity("text-embedding-3-small", 256) == "openai-compat:text-embedding-3-small@256"


def test_a_short_answer_or_a_wrong_model_is_an_error(endpoint):
    endpoint.handlers["/embeddings"] = lambda b: (200, {"data": []})
    with pytest.raises(EndpointError, match="expected 1 embeddings"):
        OpenAICompatEmbedder((endpoint.url + "/embeddings"), "", "m")
    endpoint.handlers["/embeddings"] = lambda b: (404, {"error": {"message": "model 'm' not found"}})
    with pytest.raises(EndpointError, match="HTTP 404.*not found"):
        OpenAICompatEmbedder((endpoint.url + "/embeddings"), "", "m")


def test_factory_builds_it_from_settings(endpoint):
    serve_embeddings(endpoint)
    s = replace(settings, embed_backend="openai", embed_base_url=(endpoint.url + "/embeddings"), embed_model="my-model", embed_api_key="")
    emb = get_embedder(s)
    assert emb.model_id == embedder_identity(s) == "openai-compat:my-model"
    assert embedder_identity(replace(settings, embed_backend="local", embed_model_id="BAAI/bge-small-en-v1.5")) == "BAAI/bge-small-en-v1.5"
    assert s.openai_base_url == (endpoint.url + "/embeddings") and s.openai_embed_model == "my-model"      # the older names


def test_default_model_folder_follows_the_model_name():
    assert default_model_dir("BAAI/bge-base-en-v1.5") == "models/bge-base-en-v1.5"
    assert default_model_dir("intfloat/E5-base-v2") == "models/e5-base-v2"


# ── profiles ─────────────────────────────────────────────────────────────────

def test_local_profile_takes_model_and_url_from_the_environment(monkeypatch):
    for v in ("ESAM_EMBED_MODEL", "ESAM_EMBED_BASE_URL", "ESAM_EMBED_API_KEY", "ESAM_EMBED_MODEL_DIR"):
        monkeypatch.delenv(v, raising=False)                  # a .env copied from main sets the folder itself
    prof = env.load_profile("local")
    s = env.settings_for(prof)
    assert s.embed_backend == "local" and s.embed_model_id == "BAAI/bge-base-en-v1.5"
    assert s.embed_model_dir.name == "bge-base-en-v1.5"

    monkeypatch.setenv("ESAM_EMBED_MODEL", "BAAI/bge-small-en-v1.5")                    # another local model
    s = env.settings_for(prof)
    assert s.embed_backend == "local" and s.embed_model_id == "BAAI/bge-small-en-v1.5"
    assert s.embed_model_dir.name == "bge-small-en-v1.5"

    monkeypatch.setenv("ESAM_EMBED_MODEL", "nomic-embed-text")                          # a URL: the endpoint
    monkeypatch.setenv("ESAM_EMBED_BASE_URL", "http://127.0.0.1:11434/v1/embeddings")
    monkeypatch.setenv("ESAM_EMBED_API_KEY", "sk-e")
    s = env.settings_for(prof)
    assert (s.embed_backend, s.embed_model, s.embed_base_url, s.embed_api_key) == (
        "openai", "nomic-embed-text", "http://127.0.0.1:11434/v1/embeddings", "sk-e")
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
    monkeypatch.setenv("ESAM_EMBED_BASE_URL", "https://gateway.example/v1/embeddings")
    monkeypatch.setenv("ESAM_EMBED_MODEL", "gateway-embed")
    for name in ("pilot-il5", "full-janusgraph"):
        s = env.settings_for(env.load_profile(name))
        assert (s.embed_backend, s.embed_base_url, s.embed_model) == ("openai", "https://gateway.example/v1/embeddings", "gateway-embed")


def test_probe_checks_the_endpoint_and_reports_model_and_url(endpoint):
    serve_embeddings(endpoint, dim=768)
    prof = {"name": "t", "network": {"internet": False},
            "embeddings": {"backend": "openai", "model": "my-model", "base_url": (endpoint.url + "/embeddings")}}
    s = env.settings_for(prof)
    p = env.probe_embeddings(prof, s)
    assert p.status == "ok" and "my-model" in p.detail and (endpoint.url + "/embeddings") in p.detail and p.data["dim"] == 768
    assert env.probe_embeddings(prof, s, network=False).status == "warning"
    assert endpoint.paths() == ["/embeddings"]                    # --no-network contacted nothing

    down = dict(prof, embeddings=dict(prof["embeddings"], base_url=dead_url()))
    assert env.probe_embeddings(down, env.settings_for(down)).status == "unavailable"
    no_model = dict(prof, embeddings={"backend": "openai", "base_url": (endpoint.url + "/embeddings")})
    p = env.probe_embeddings(no_model, replace(env.settings_for(no_model), embed_model=""))
    assert p.status == "not_configured" and "embeddings.model" in p.detail
    no_url = dict(prof, embeddings={"backend": "openai", "model": "m", "base_url_env": "T_EMBED_URL"})
    p = env.probe_embeddings(no_url, replace(env.settings_for(no_url), embed_base_url=""))
    assert p.status == "not_configured" and "T_EMBED_URL" in p.detail
    public = dict(prof, embeddings={"backend": "openai", "model": "m", "base_url": "https://api.openai.com/v1"})
    assert "air-gapped" in env.probe_embeddings(public, env.settings_for(public)).detail


# ── what differed from main (the OpenAI SDK client) ──────────────────────────

def test_dotenv_values_may_be_followed_by_a_comment():
    from esamkit.config import _dotenv_line as line
    assert line("ESAM_EMBED_API_KEY=sk-abc123        # the gateway's key") == ("ESAM_EMBED_API_KEY", "sk-abc123")
    assert line("ESAM_EMBED_BASE_URL=https://gw.example/v1   # ours") == ("ESAM_EMBED_BASE_URL", "https://gw.example/v1")
    assert line("ESAM_EMBED_DIMENSIONS=0                # shorter vectors") == ("ESAM_EMBED_DIMENSIONS", "0")
    assert line("ESAM_EMBED_BACKEND=                    # local | openai") == ("ESAM_EMBED_BACKEND", "")
    assert line('KEY="a value # with a hash"  # note') == ("KEY", "a value # with a hash")
    assert line("URL=https://host/v1#frag") == ("URL", "https://host/v1#frag")          # no space before '#': part of the value
    assert line("export ESAM_PG_DSN='postgresql://u:p@h/db'") == ("ESAM_PG_DSN", "postgresql://u:p@h/db")
    assert line("# ESAM_EMBED_API_KEY=x") is None and line("") is None and line("no equals sign") is None


def test_every_line_of_the_example_file_can_be_uncommented():
    import re
    from esamkit.config import PROJECT_ROOT, _dotenv_line as line
    for raw in (PROJECT_ROOT / ".env.example").read_text().splitlines():
        m = re.match(r"^#?\s*(ESAM_[A-Z0-9_]+=.*)$", raw)
        if m:
            key, value = line(m[1])
            assert "#" not in value and value == value.strip(), raw
            if key in ("ESAM_EMBED_DIMENSIONS", "ESAM_EMBED_BATCH_SIZE", "ESAM_CHUNK_SIZE", "ESAM_CHUNK_OVERLAP",
                       "ESAM_LLM_TIMEOUT", "ESAM_LLM_FAILOVER_COOLDOWN"):
                float(value)


def test_an_endpoint_set_up_with_the_older_names_works_under_a_profile(monkeypatch, endpoint):
    # main's names: ESAM_EMBED_BACKEND=openai + ESAM_OPENAI_*; ESAM_EMBED_MODEL_ID stays the local model's id
    serve_embeddings(endpoint, dim=4)
    for v in ("ESAM_EMBED_MODEL", "ESAM_EMBED_BASE_URL", "ESAM_EMBED_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("ESAM_EMBED_BACKEND", "openai")
    monkeypatch.setenv("ESAM_OPENAI_BASE_URL", (endpoint.url + "/embeddings"))
    monkeypatch.setenv("ESAM_OPENAI_API_KEY", "sk-old")
    monkeypatch.setenv("ESAM_OPENAI_EMBED_MODEL", "gateway-embed")
    monkeypatch.setenv("ESAM_EMBED_MODEL_ID", "BAAI/bge-base-en-v1.5")
    for name in ("local", "pilot-il5"):
        choice = env.embedding_choice(env.load_profile(name))
        assert (choice["backend"], choice["model"], choice["base_url"], choice["api_key"]) == ("openai", "gateway-embed", (endpoint.url + "/embeddings"), "sk-old")
        assert choice["model_source"] == "env ESAM_OPENAI_EMBED_MODEL"
        s = env.settings_for(env.load_profile(name))
        emb = get_embedder(s)
        assert emb.model_id == "openai-compat:gateway-embed" and endpoint.calls[-1][1]["model"] == "gateway-embed"
        assert endpoint.calls[-1][2]["Authorization"] == "Bearer sk-old"


def test_requests_do_not_announce_themselves_as_python_urllib(endpoint):
    serve_embeddings(endpoint, dim=4)
    OpenAICompatEmbedder((endpoint.url + "/embeddings"), "k", "m")
    assert endpoint.calls[0][2]["User-Agent"] == "ESAM/1.0"


def test_a_passing_failure_is_tried_again_and_a_refused_key_is_not(endpoint, monkeypatch):
    from esamkit.embedders import openai_compat
    from esamkit.llm import EndpointError
    monkeypatch.setattr(openai_compat, "RETRY_WAIT", 0)
    answers = [(503, {"error": "warming up"}), (429, {"error": "slow down"}),
               (200, {"data": [{"index": 0, "embedding": [0.0, 1.0]}]})]
    endpoint.handlers["/embeddings"] = lambda b: answers.pop(0) if len(answers) > 1 else answers[0]
    assert OpenAICompatEmbedder((endpoint.url + "/embeddings"), "k", "m").dim == 2 and len(endpoint.calls) == 3

    endpoint.calls.clear()
    endpoint.handlers["/embeddings"] = lambda b: (401, {"error": {"message": "Incorrect API key"}})
    with pytest.raises(EndpointError) as err:
        OpenAICompatEmbedder((endpoint.url + "/embeddings"), "sk-abcdefghijklmnopqrstuvwx", "m")
    assert "Bearer <key from ESAM_EMBED_API_KEY: 27 characters, ends …uvwx>" in str(err.value)
    assert "sk-abcdefghij" not in str(err.value) and "Incorrect API key" in str(err.value)
    with pytest.raises(EndpointError, match="it contains spaces, a '#': check the line in .env"):
        OpenAICompatEmbedder((endpoint.url + "/embeddings"), "sk-abc   # my key", "m")
    with pytest.raises(EndpointError, match="No API key was set; set ESAM_EMBED_API_KEY"):
        OpenAICompatEmbedder((endpoint.url + "/embeddings"), "", "m")
    assert len(endpoint.calls) == 3                                  # one each: not retried

    endpoint.calls.clear()
    endpoint.handlers["/embeddings"] = lambda b: (404, {"error": {"message": "model not found"}})
    with pytest.raises(EndpointError, match="model not found"):
        OpenAICompatEmbedder((endpoint.url + "/embeddings"), "k", "nope")
    assert len(endpoint.calls) == 1


def test_one_gateway_for_both_uses_the_reasoning_key_when_embeddings_has_none(endpoint, endpoint_b):
    from esamkit.factory import embed_api_key
    serve_embeddings(endpoint, dim=4)
    serve_embeddings(endpoint_b, dim=4)
    s = replace(settings, embed_backend="openai", embed_base_url=(endpoint.url + "/embeddings"), embed_model="m", embed_api_key="",
                chat_base_url=endpoint.url.replace("/v1", "/openai/v1"), chat_api_key="gateway-key")
    get_embedder(s)
    assert endpoint.calls[-1][2]["Authorization"] == "Bearer gateway-key"          # same host: the gateway's one key
    assert "same host as the reasoning model" in embed_api_key(s)[1]
    get_embedder(replace(s, embed_api_key="embed-key"))
    assert endpoint.calls[-1][2]["Authorization"] == "Bearer embed-key"            # its own key always wins
    get_embedder(replace(s, embed_base_url=(endpoint_b.url + "/embeddings")))
    assert "Authorization" not in endpoint_b.calls[-1][2]                          # another host never gets the reasoning key
    assert embed_api_key(replace(s, chat_api_key="")) == ("", "")


def test_the_request_goes_to_base_url_plus_embeddings_as_on_main(endpoint):
    from esamkit.embedders.openai_compat import base_of
    serve_embeddings(endpoint, dim=4)
    for given in (endpoint.url, endpoint.url + "/", endpoint.url + "/embeddings", endpoint.url + "/embeddings/"):
        endpoint.calls.clear()
        emb = OpenAICompatEmbedder(given, "k", "m", client="http")
        assert endpoint.paths() == ["/embeddings"] and emb.base_url == endpoint.url        # .../v1 and .../v1/embeddings: one address
    assert base_of("https://gw.example/v1") == base_of("https://gw.example/v1/embeddings/") == "https://gw.example/v1"
    assert base_of("https://gw.example/embeddings-api/v1") == "https://gw.example/embeddings-api/v1"


# ── the OpenAI SDK, as main calls it ─────────────────────────────────────────

class FakeSDK:
    """Stands in for the `openai` package: records how it is called and
    forwards the request to the fake endpoint, as the real SDK would."""
    made = []

    class AuthenticationError(Exception):
        status_code = 401

    def __init__(self, **kw):
        self.kw, self.requests = kw, []
        FakeSDK.made.append(self)
        self.embeddings = self

    def create(self, **kw):
        from types import SimpleNamespace as NS
        from esamkit.llm import EndpointError, post_json
        self.requests.append(kw)
        try:
            data = post_json(self.kw["base_url"].rstrip("/") + "/embeddings", kw, self.kw["api_key"])
        except EndpointError as e:
            if e.status == 401:
                raise FakeSDK.AuthenticationError("Error code: 401 - Incorrect API key") from None
            raise
        return NS(data=[NS(index=d["index"], embedding=d["embedding"]) for d in data["data"]])


@pytest.fixture
def sdk(monkeypatch):
    import sys
    import types
    FakeSDK.made.clear()
    module = types.ModuleType("openai")
    module.OpenAI = FakeSDK
    monkeypatch.setitem(sys.modules, "openai", module)
    return FakeSDK


def test_with_the_sdk_installed_it_is_called_exactly_as_main_calls_it(endpoint, sdk):
    serve_embeddings(endpoint, dim=5, reverse=True)
    emb = OpenAICompatEmbedder(endpoint.url, "sk-main", "gateway-embed")              # client="auto": the SDK is there
    assert emb.client == "sdk" and emb.dim == 5 and emb.model_id == "openai-compat:gateway-embed"
    client = sdk.made[0]
    assert client.kw["base_url"] == endpoint.url and client.kw["api_key"] == "sk-main"   # main: OpenAI(base_url=..., api_key=...)
    assert client.requests[0] == {"model": "gateway-embed", "input": ["dimension probe"]}   # main: embeddings.create(model=, input=)
    vectors = emb.embed_documents(["a", "bbb", "cc"])
    assert [v[0] for v in vectors] == [1.0, 3.0, 2.0]                                # answers re-ordered by index
    assert endpoint.paths() == ["/embeddings"] * 2 and endpoint.calls[0][2]["Authorization"] == "Bearer sk-main"

    OpenAICompatEmbedder(endpoint.url + "/embeddings", "", "m", dimensions=3)            # full URL: same base; no key: "unused", as main
    assert sdk.made[1].kw["base_url"] == endpoint.url and sdk.made[1].kw["api_key"] == "unused"
    assert sdk.made[1].requests[0] == {"model": "m", "input": ["dimension probe"], "dimensions": 3}


def test_the_client_can_be_chosen(endpoint, sdk, monkeypatch):
    import sys
    serve_embeddings(endpoint, dim=4)
    assert OpenAICompatEmbedder(endpoint.url, "k", "m", client="http").client == "http" and sdk.made == []
    s = replace(settings, embed_backend="openai", embed_base_url=endpoint.url, embed_model="m", embed_api_key="k")
    assert get_embedder(replace(s, embed_client="sdk")).client == "sdk"
    assert get_embedder(replace(s, embed_client="http")).client == "http"
    with pytest.raises(ValueError, match="use auto, sdk or http"):
        OpenAICompatEmbedder(endpoint.url, "k", "m", client="curl")
    monkeypatch.setitem(sys.modules, "openai", None)                                  # the package is not installed
    assert OpenAICompatEmbedder(endpoint.url, "k", "m").client == "http"             # auto: the built-in client
    with pytest.raises(ImportError, match="needs the openai package"):
        OpenAICompatEmbedder(endpoint.url, "k", "m", client="sdk")


def test_a_401_through_the_sdk_says_what_was_sent(endpoint, sdk):
    endpoint.handlers["/embeddings"] = lambda b: (401, {"error": {"message": "Incorrect API key"}})
    with pytest.raises(EndpointError) as err:
        OpenAICompatEmbedder(endpoint.url, "sk-abcdefghijklmnopqrstuvwx", "m")
    assert err.value.status == 401 and "Bearer <key from ESAM_EMBED_API_KEY: 27 characters, ends …uvwx>" in str(err.value)
    assert "sk-abcdefghij" not in str(err.value)


def test_other_sdk_errors_become_endpoint_errors(endpoint, sdk, monkeypatch):
    class NotFoundError(Exception):
        status_code = 404

    def wrong_model(self, **kw):
        raise NotFoundError("Error code: 404 - model not found")

    def empty_answer(self, **kw):
        raise ValueError("No embedding data received")             # what the real SDK raises for "data": []

    monkeypatch.setattr(sdk, "create", wrong_model)
    with pytest.raises(EndpointError, match="model not found") as err:
        OpenAICompatEmbedder(endpoint.url, "k", "nope")
    assert err.value.status == 404 and "'nope'" in str(err.value)
    monkeypatch.setattr(sdk, "create", empty_answer)
    with pytest.raises(EndpointError, match="No embedding data received") as err:
        OpenAICompatEmbedder(endpoint.url, "k", "m")
    assert err.value.status is None


@pytest.mark.skipif(REAL_OPENAI is None, reason="the openai package is not installed")
def test_the_real_sdk_reports_a_wrong_model_and_an_empty_answer_as_endpoint_errors(endpoint, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "openai", REAL_OPENAI)
    endpoint.handlers["/embeddings"] = lambda b: (404, {"error": {"message": "model not found"}})
    with pytest.raises(EndpointError, match="model not found") as err:
        OpenAICompatEmbedder(endpoint.url, "k", "nope")
    assert err.value.status == 404 and len(endpoint.calls) == 1   # a wrong model is not tried again
    endpoint.handlers["/embeddings"] = lambda b: (200, {"data": []})
    with pytest.raises(EndpointError, match="No embedding data received"):
        OpenAICompatEmbedder(endpoint.url, "k", "m")

