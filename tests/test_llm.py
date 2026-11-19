"""Model endpoints (esamkit.llm): the Chat Completions and Responses APIs,
fallback endpoints, and what counts as a failure worth failing over.
Everything runs against a fake endpoint on 127.0.0.1 (tests/conftest.py)."""

from dataclasses import replace

import pytest

from conftest import chat_answer, dead_url, responses_answer
from esamkit import environment as env
from esamkit import llm
from esamkit.config import _fallbacks, settings


@pytest.fixture(autouse=True)
def fresh_failover_state():
    llm._CHAINS.clear()          # which routes are "down" is remembered per process
    yield
    llm._CHAINS.clear()


def routes(*specs, **primary):
    return llm.build_routes(primary, list(specs))


# ── routes ───────────────────────────────────────────────────────────────────

def test_a_fallback_states_only_what_differs():
    rs = routes({"api": "responses"}, {"base_url": "https://b.example/v1/", "api_key": "kb"},
                base_url="https://a.example/v1", model="gpt-5.1", api_key="ka", api="chat")
    assert [(r.base_url, r.model, r.api_key, r.api) for r in rs] == [
        ("https://a.example/v1", "gpt-5.1", "ka", "chat"),
        ("https://a.example/v1", "gpt-5.1", "ka", "responses"),
        ("https://b.example/v1", "gpt-5.1", "kb", "chat")]
    assert "ka" not in str([r.describe() for r in rs])            # keys are never in what gets logged


def test_auto_is_chat_then_responses_and_duplicates_are_dropped():
    rs = routes({"api": "responses"}, {"api": "chat"}, base_url="https://a.example/v1", model="m", api="auto")
    assert [r.api for r in rs] == ["chat", "responses"]
    assert routes(base_url="", model="m") == [] and routes(base_url="https://a/v1", model="") == []
    with pytest.raises(ValueError, match="chat, responses or auto"):
        routes(base_url="https://a/v1", model="m", api="graphql")
    assert llm.normalize_api("Chat-Completions") == "chat" and llm.normalize_api(None) == "chat"


def test_fallbacks_from_the_environment(monkeypatch):
    monkeypatch.setenv("X_FALLBACK_1_API", "responses")
    monkeypatch.setenv("X_FALLBACK_2_BASE_URL", "https://b.example/v1")
    monkeypatch.setenv("X_FALLBACK_2_MODEL", "other")
    monkeypatch.setenv("X_FALLBACK_4_API", "chat")               # numbering stops at the first gap
    fbs = [dict(f) for f in _fallbacks("X")]
    assert fbs == [{"api": "responses"}, {"base_url": "https://b.example/v1", "model": "other"}]
    s = replace(settings, chat_base_url="https://a.example/v1", chat_model="m", chat_api="chat", chat_fallbacks=_fallbacks("X"))
    assert [(r.base_url, r.model, r.api) for r in llm.chat_routes(s)] == [
        ("https://a.example/v1", "m", "chat"), ("https://a.example/v1", "m", "responses"),
        ("https://b.example/v1", "other", "chat")]


# ── what is worth failing over ───────────────────────────────────────────────

class SdkError(Exception):
    def __init__(self, status_code, message=""):
        super().__init__(message)
        self.status_code = status_code


class APIConnectionError(Exception):
    pass


@pytest.mark.parametrize("exc, expected", [
    (llm.EndpointError("u", 404, "Not Found"), True),
    (llm.EndpointError("u", 405, ""), True),
    (llm.EndpointError("u", 429, "rate limit"), True),
    (llm.EndpointError("u", 503, ""), True),
    (llm.EndpointError("u", 401, "bad key"), True),
    (llm.EndpointError("u", None, "connection refused"), True),
    (llm.EndpointError("u", 400, "This model is only supported in v1/responses and not in v1/chat/completions."), True),
    (llm.EndpointError("u", 400, "Invalid value for 'temperature'"), False),
    (llm.EndpointError("u", 400, "context_length_exceeded: maximum context length is 400000 tokens"), False),
    (llm.EndpointError("u", 400, "Your request was rejected by the content_filter"), False),
    (SdkError(500), True), (SdkError(404), True), (SdkError(422, "bad schema"), False),
    (APIConnectionError("Connection error."), True), (TimeoutError(), True),
    (ValueError("the agent's own bug"), False), (KeyError("x"), False),
])
def test_should_failover(exc, expected):
    assert llm.should_failover(exc) is expected


def test_should_failover_looks_through_wrapped_errors():
    try:
        try:
            raise SdkError(502, "bad gateway")
        except SdkError as e:
            raise RuntimeError("OpenAI API call failed") from e
    except RuntimeError as wrapped:
        assert llm.should_failover(wrapped)


# ── calls ────────────────────────────────────────────────────────────────────

def test_chat_completions_request_and_answer(endpoint):
    endpoint.handlers["/chat/completions"] = lambda b: (200, chat_answer("42"))
    [r] = routes(base_url=endpoint.url, model="gpt-5.1", api_key="sk-test")
    text, usage = llm.request_text(r, "be brief", "what?", images=[("image/png", b"\x89PNG")])
    path, body, headers = endpoint.calls[0]
    assert text == "42" and usage == {"total_tokens": 3} and path == "/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test" and body["model"] == "gpt-5.1"
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    kinds = [p["type"] for p in body["messages"][1]["content"]]
    assert kinds == ["text", "image_url"] and body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_responses_api_request_and_answer(endpoint):
    endpoint.handlers["/responses"] = lambda b: (200, responses_answer("forty-two"))
    [r] = routes(base_url=endpoint.url, model="gpt-5.1", api="responses")
    text, _ = llm.request_text(r, "be brief", "what?", images=[("image/jpeg", b"\xff\xd8")])
    path, body, headers = endpoint.calls[0]
    assert text == "forty-two" and path == "/responses" and "Authorization" not in headers
    assert body["instructions"] == "be brief" and body["input"][0]["role"] == "user"
    assert [p["type"] for p in body["input"][0]["content"]] == ["input_text", "input_image"]
    assert body["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")


def _settings(url, **kw):
    return replace(settings, **{"chat_base_url": url, "chat_model": "gpt-5.1", "chat_api_key": "k", "chat_api": "chat",
                                "chat_fallbacks": (), "llm_timeout": 5, **kw})


def test_chat_fails_so_the_responses_api_answers(endpoint):
    """The endpoint serves the model only on /responses: /chat/completions is a 404."""
    endpoint.handlers["/responses"] = lambda b: (200, responses_answer("from responses"))
    s = _settings(endpoint.url, chat_api="auto")
    assert llm.complete("sys", "hello", s=s) == "from responses"
    assert endpoint.paths() == ["/chat/completions", "/responses"]
    # the dead route is remembered: the next call goes straight to the one that works
    assert llm.complete("sys", "again", s=s) == "from responses"
    assert endpoint.paths()[2:] == ["/responses"]


def test_a_dead_endpoint_falls_back_to_another_endpoint(endpoint):
    endpoint.handlers["/chat/completions"] = lambda b: (200, chat_answer("from the second endpoint"))
    s = replace(_settings(dead_url()), chat_fallbacks=((("api_key", "k2"), ("base_url", endpoint.url)),))
    assert llm.complete("", "hello", s=s) == "from the second endpoint"
    assert endpoint.calls[0][2]["Authorization"] == "Bearer k2"


def test_server_errors_fail_over_but_a_bad_request_does_not(endpoint, endpoint_b):
    endpoint.handlers["/chat/completions"] = lambda b: (503, {"error": {"message": "overloaded"}})
    endpoint_b.handlers["/chat/completions"] = lambda b: (200, chat_answer("b"))
    s = replace(_settings(endpoint.url), chat_fallbacks=((("base_url", endpoint_b.url),),))
    assert llm.complete("", "x", s=s) == "b"

    endpoint.calls.clear(), endpoint_b.calls.clear(), llm._CHAINS.clear()
    endpoint.handlers["/chat/completions"] = lambda b: (400, {"error": {"code": "context_length_exceeded",
                                                                         "message": "maximum context length exceeded"}})
    s2 = replace(_settings(endpoint.url), chat_fallbacks=((("base_url", endpoint_b.url),),))
    with pytest.raises(llm.EndpointError) as e:
        llm.complete("", "x", s=s2)
    assert e.value.status == 400 and endpoint_b.calls == []       # the same request would fail there too


def test_when_every_route_fails_the_primary_error_is_raised(endpoint):
    endpoint.handlers["/chat/completions"] = lambda b: (500, {"error": "boom"})
    s = replace(_settings(endpoint.url), chat_fallbacks=((("api", "responses"),), (("base_url", dead_url()),)))
    with pytest.raises(llm.EndpointError) as e:
        llm.complete("", "x", s=s)
    assert e.value.status == 500 and "/chat/completions" in e.value.url
    assert [name.split(":")[1] for name, _ in e.value.failover_errors] == ["chat", "responses", "chat"]


def test_an_answer_without_text_counts_as_a_failure(endpoint):
    endpoint.handlers["/chat/completions"] = lambda b: (200, {"choices": []})
    endpoint.handlers["/responses"] = lambda b: (200, responses_answer("ok"))
    assert llm.complete("", "x", s=_settings(endpoint.url, chat_api="auto")) == "ok"


def test_cooldown_skips_a_failed_route_then_tries_it_again():
    now = [0.0]
    a, b = routes({"base_url": "https://b/v1"}, base_url="https://a/v1", model="m")
    fo = llm.Failover([a, b], cooldown=60, clock=lambda: now[0])
    seen = []

    def attempt(route):
        seen.append(route.base_url)
        if route is a and now[0] < 100:
            raise llm.EndpointError(route.base_url, 503, "down")
        return route.base_url

    assert fo.run(attempt) == "https://b/v1" and seen == ["https://a/v1", "https://b/v1"]
    assert fo.run(attempt) == "https://b/v1" and seen[2:] == ["https://b/v1"]          # a is cooling down
    now[0] = 61
    assert fo.run(attempt) == "https://b/v1" and seen[3:] == ["https://a/v1", "https://b/v1"]   # tried again, still down
    now[0] = 200
    assert fo.run(attempt) == "https://a/v1"                                           # and back
    rep = fo.summary()
    assert rep["used_fallback"] and rep["served"] == {a.name: 1, b.name: 3} and len(rep["failures"]) == 2


def test_errors_that_are_not_endpoint_failures_pass_straight_through():
    a, b = routes({"base_url": "https://b/v1"}, base_url="https://a/v1", model="m")
    fo, tried = llm.Failover([a, b]), []

    def attempt(route):
        tried.append(route)
        raise ValueError("a bug in the caller")

    with pytest.raises(ValueError):
        fo.run(attempt)
    assert tried == [a] and fo.failures == []


def test_chat_module_still_answers(endpoint):
    from esamkit.chat import chat_configured, complete
    endpoint.handlers["/chat/completions"] = lambda b: (200, chat_answer("hi"))
    s = _settings(endpoint.url)
    assert chat_configured(s) and complete("sys", "user", s) == "hi"
    assert not chat_configured(replace(s, chat_model=""))


# ── profiles ─────────────────────────────────────────────────────────────────

def _profile(**reasoning):
    return {"name": "t", "network": {"internet": False},
            "llm": {"roles": {"reasoning": {"model": "gpt-5.1", "base_url_env": "T_URL", "api_key_env": "T_KEY", **reasoning}}}}


def test_profile_routes_fallbacks_and_skipped_ones(monkeypatch):
    monkeypatch.setenv("T_URL", "https://gw.example/v1")
    monkeypatch.setenv("T_KEY", "k")
    monkeypatch.setenv("T_URL_B", "https://gw-b.example/v1")
    monkeypatch.delenv("T_URL_C", raising=False)
    prof = _profile(api="auto", fallbacks=[{"base_url_env": "T_URL_B", "model": "gpt-5.1-mini", "api": "responses"},
                                           {"base_url_env": "T_URL_C"}])
    rs = env.role_routes(prof, "reasoning")
    assert [(r.base_url, r.model, r.api) for r in rs] == [
        ("https://gw.example/v1", "gpt-5.1", "chat"), ("https://gw.example/v1", "gpt-5.1", "responses"),
        ("https://gw-b.example/v1", "gpt-5.1-mini", "responses")]
    role = env.resolve(prof)["roles"]["reasoning"]
    assert role["api"] == "auto" and role["fallbacks_skipped"] == ["fallback 2: set T_URL_C"]
    assert "'k'" not in str(role)                                # resolve() never carries a key
    s = env.settings_for(prof)
    assert s.chat_api == "auto" and [(r.base_url, r.api) for r in llm.chat_routes(s)] == [(r.base_url, r.api) for r in rs]


def test_vision_role_inherits_the_reasoning_endpoint(monkeypatch):
    monkeypatch.setenv("T_URL", "https://gw.example/v1")
    monkeypatch.setenv("T_KEY", "k")
    prof = _profile(api="responses")
    base = replace(settings, vision_model="")
    assert llm.vision_routes(env.settings_for(prof, base)) == []          # no vision role: no picture is ever sent
    prof["llm"]["roles"]["vision"] = {"model": "gpt-5.1-vision"}
    [r] = llm.vision_routes(env.settings_for(prof))
    assert (r.base_url, r.model, r.api_key, r.api) == ("https://gw.example/v1", "gpt-5.1-vision", "k", "responses")


def test_shipped_profiles_keep_one_route_until_fallbacks_are_uncommented(monkeypatch):
    monkeypatch.setenv("ESAM_CHAT_API_KEY", "k")
    rs = env.role_routes(env.load_profile("local"), "reasoning")
    assert [(r.base_url, r.model, r.api) for r in rs] == [("https://api.openai.com/v1", "gpt-5.1", "chat")]


def test_probe_reports_a_usable_fallback_as_a_warning(monkeypatch, endpoint):
    endpoint.handlers["/models"] = lambda b: (200, {"data": [{"id": "gpt-5.1"}]})
    monkeypatch.setenv("T_URL", dead_url())
    monkeypatch.setenv("T_KEY", "k")
    monkeypatch.setenv("T_URL_B", endpoint.url)
    prof = _profile(fallbacks=[{"base_url_env": "T_URL_B"}])
    p = env.probe_llm(prof, env.resolve(prof), "reasoning", network=True)
    assert p.status == "warning" and "calls will go to a fallback" in p.detail and p.data["fallbacks"][0]["status"] == "ok"
    assert env.capabilities([p])["agents_can_run"]
    monkeypatch.setenv("T_URL_B", dead_url())
    assert env.probe_llm(prof, env.resolve(prof), "reasoning", network=True).status == "unavailable"
