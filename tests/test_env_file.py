"""The environment file and the profiles agree: every variable a profile
names is in .env.example, every model/endpoint variable in .env.example is
read, and reasoning and coding have separate endpoints."""

import re
from dataclasses import replace

import pytest
import yaml

from esamkit import config, environment as env, llm
from esamkit.config import PROJECT_ROOT, settings

EXAMPLE = (PROJECT_ROOT / ".env.example").read_text()
DOCUMENTED = set(re.findall(r"^#?\s*(ESAM_[A-Z0-9_]+)=", EXAMPLE, re.M))
PROFILES = sorted((PROJECT_ROOT / "config" / "environments").glob("*.yaml"))
ROLE_VARS = [f"ESAM_{role}_{var}" for role in ("REASONING", "CODING", "VISION") for var in ("BASE_URL", "API_KEY", "MODEL", "API")]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    import os
    for k in list(os.environ):
        if k.startswith("ESAM_"):
            monkeypatch.delenv(k)


def named(node, out=None):
    """Every variable a profile names: values of keys ending in _env."""
    out = set() if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if k.endswith("_env") and isinstance(v, str):
                out.add(v)
            named(v, out)
    elif isinstance(node, list):
        for v in node:
            named(v, out)
    return out


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_every_variable_a_profile_names_is_in_the_environment_file(path):
    names = named(yaml.safe_load(path.read_text()))
    assert names, "the profile names no variables at all"
    for name in names:
        if name.endswith("_FALLBACK"):                              # a prefix: ESAM_X_FALLBACK_<n>_BASE_URL ...
            assert any(d.startswith(name + "_") for d in DOCUMENTED), name
        else:
            assert name in DOCUMENTED, f"{path.name} names {name}, which .env.example does not mention"


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_every_role_variable_in_the_environment_file_reaches_the_profile(path, monkeypatch):
    assert set(ROLE_VARS) <= DOCUMENTED
    prof = yaml.safe_load(path.read_text())
    assert set(ROLE_VARS) <= named(prof), sorted(set(ROLE_VARS) - named(prof))
    for role in ("reasoning", "coding", "vision"):
        up = role.upper()
        monkeypatch.setenv(f"ESAM_{up}_BASE_URL", f"https://{role}.example/v1")
        monkeypatch.setenv(f"ESAM_{up}_API_KEY", f"{role}-key")
        monkeypatch.setenv(f"ESAM_{up}_MODEL", f"{role}-model")
        monkeypatch.setenv(f"ESAM_{up}_API", "responses")
    for role in ("reasoning", "coding", "vision"):
        [r] = env.role_routes(prof, role)
        assert (r.base_url, r.model, r.api_key, r.api) == (f"https://{role}.example/v1", f"{role}-model", f"{role}-key", "responses")
    s = env.settings_for(prof)
    assert (s.chat_base_url, s.chat_model, s.chat_api_key, s.chat_api) == ("https://reasoning.example/v1", "reasoning-model", "reasoning-key", "responses")
    assert (s.coding_base_url, s.coding_model, s.coding_api_key, s.coding_api) == ("https://coding.example/v1", "coding-model", "coding-key", "responses")
    assert [r.base_url for r in llm.coding_routes(s)] == ["https://coding.example/v1"]
    assert [r.base_url for r in llm.vision_routes(s)] == ["https://vision.example/v1"]


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_the_other_settings_in_the_environment_file_reach_the_profile(path, monkeypatch):
    prof = yaml.safe_load(path.read_text())
    monkeypatch.setenv("ESAM_COLLECTION", "my_collection")
    monkeypatch.setenv("ESAM_DIAGRAMS", "off")
    monkeypatch.setenv("ESAM_EMBED_MODEL", "nomic-embed-text")
    monkeypatch.setenv("ESAM_EMBED_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("ESAM_EMBED_API_KEY", "ek")
    monkeypatch.setenv("ESAM_PG_DSN", "postgresql://u:p@db.example:5432/esam")
    s = env.settings_for(prof)
    assert (s.collection, s.diagrams, s.pg_dsn) == ("my_collection", "off", "postgresql://u:p@db.example:5432/esam")
    assert (s.embed_backend, s.embed_model, s.embed_base_url, s.embed_api_key) == ("openai", "nomic-embed-text", "http://127.0.0.1:11434/v1", "ek")
    monkeypatch.delenv("ESAM_EMBED_BASE_URL")
    monkeypatch.setenv("ESAM_EMBED_MODEL_DIR", "/srv/models/nomic")
    s = env.settings_for(prof)
    assert s.embed_backend == "local" and str(s.embed_model_dir) == "/srv/models/nomic"


def test_nothing_set_gives_the_profiles_own_defaults():
    local = env.load_profile("local")
    [r] = env.role_routes(local, "reasoning")
    assert (r.base_url, r.model, r.api, r.api_key) == ("https://api.openai.com/v1", "gpt-5.1", "chat", "")
    assert env.role_routes(local, "coding") == [] and env.role_routes(local, "vision") == []   # optional roles are off
    roles = env.resolve(local)["roles"]
    assert roles["coding"] is None and roles["vision"] is None and roles["reasoning"]["model_env"] == "ESAM_REASONING_MODEL"
    s = env.settings_for(local, replace(settings, vision_model="", coding_model=""))
    assert s.vision_model == "" and s.coding_model == "" and s.collection == "esam_documents" and s.diagrams == "auto"
    pilot = env.load_profile("pilot-il5")
    assert env.resolve(pilot)["roles"]["coding"]["model"] == "nemotron-4-15b"
    assert env.role_routes(pilot, "coding") == [] and env.settings_for(pilot).diagrams == "local"     # a model, but no URL yet


def test_reasoning_and_coding_share_nothing(monkeypatch):
    pilot = env.load_profile("pilot-il5")
    monkeypatch.setenv("ESAM_REASONING_BASE_URL", "https://reason.example/v1")
    monkeypatch.setenv("ESAM_REASONING_API_KEY", "rk")
    monkeypatch.setenv("ESAM_REASONING_API", "auto")
    monkeypatch.setenv("ESAM_REASONING_FALLBACK_1_BASE_URL", "https://reason-b.example/v1")
    assert [(r.base_url, r.api) for r in env.role_routes(pilot, "reasoning")] == [
        ("https://reason.example/v1", "chat"), ("https://reason.example/v1", "responses"),
        ("https://reason-b.example/v1", "chat"), ("https://reason-b.example/v1", "responses")]
    assert env.role_routes(pilot, "coding") == []                    # reasoning's URL, key, API and fallbacks are not coding's
    p = env.probe_llm(pilot, env.resolve(pilot), "coding", network=False)
    assert p.status == "not_configured" and "ESAM_CODING_BASE_URL" in p.detail

    monkeypatch.setenv("ESAM_CODING_BASE_URL", "https://code.example/v1")
    monkeypatch.setenv("ESAM_CODING_API_KEY", "ck")
    monkeypatch.setenv("ESAM_CODING_FALLBACK_1_API", "responses")
    assert [(r.base_url, r.model, r.api, r.api_key) for r in env.role_routes(pilot, "coding")] == [
        ("https://code.example/v1", "nemotron-4-15b", "chat", "ck"), ("https://code.example/v1", "nemotron-4-15b", "responses", "ck")]
    assert all("code.example" not in r.base_url for r in env.role_routes(pilot, "reasoning"))
    # vision is the one role that borrows: with only a model it uses reasoning's endpoint and fallbacks
    monkeypatch.setenv("ESAM_VISION_MODEL", "see-1")
    assert {(r.base_url, r.model) for r in env.role_routes(pilot, "vision")} == {
        ("https://reason.example/v1", "see-1"), ("https://reason-b.example/v1", "see-1")}


def test_older_variable_names_are_still_read(monkeypatch):
    assert config.env_names("ESAM_REASONING_BASE_URL") == ["ESAM_REASONING_BASE_URL", "ESAM_CHAT_BASE_URL", "ESAM_GATEWAY_URL"]
    assert config.env_names("ESAM_CODING_API_KEY") == ["ESAM_CODING_API_KEY", "ESAM_GATEWAY_KEY"]
    assert config.env_names("ESAM_REASONING_FALLBACK_2_API") == ["ESAM_REASONING_FALLBACK_2_API", "ESAM_CHAT_FALLBACK_2_API"]
    monkeypatch.setenv("ESAM_CHAT_API_KEY", "old-key")
    monkeypatch.setenv("ESAM_CHAT_MODEL", "old-model")
    monkeypatch.setenv("ESAM_CHAT_FALLBACK_1_API", "responses")
    rs = env.role_routes(env.load_profile("local"), "reasoning")
    assert [(r.model, r.api, r.api_key) for r in rs] == [("old-model", "chat", "old-key"), ("old-model", "responses", "old-key")]
    assert config.env_source("ESAM_REASONING_API_KEY") == "ESAM_CHAT_API_KEY"
    monkeypatch.setenv("ESAM_REASONING_MODEL", "new-model")           # the current name wins
    assert env.role_routes(env.load_profile("local"), "reasoning")[0].model == "new-model"
    # one gateway setting used to serve both roles
    monkeypatch.setenv("ESAM_GATEWAY_URL", "https://gw.example/v1")
    monkeypatch.setenv("ESAM_GATEWAY_KEY", "gk")
    pilot = env.load_profile("pilot-il5")
    assert env.role_routes(pilot, "coding")[0].base_url == "https://gw.example/v1"
    assert env.role_routes(pilot, "coding")[0].api_key == "gk" and env.role_routes(pilot, "reasoning")[0].api_key == "old-key"


def test_a_profile_written_the_old_way_still_works(monkeypatch):
    monkeypatch.setenv("T_URL", "https://gw.example/v1")
    monkeypatch.setenv("T_KEY", "k")
    old = {"name": "t", "llm": {"roles": {"reasoning": {"model": "gpt-5.1", "base_url_env": "T_URL", "api_key_env": "T_KEY"},
                                          "code": {"model": "nemotron-4-15b", "base_url_env": "T_URL", "api_key_env": "T_KEY"},
                                          "vision": {"model": "gpt-5.1"}}}}
    assert env.role_routes(old, "coding")[0].model == "nemotron-4-15b"       # `code` is the older name of the role
    assert set(env.resolve(old)["roles"]) == {"reasoning", "coding", "vision"}
    assert env.role_routes(old, "vision")[0].base_url == "https://gw.example/v1"


def test_no_comment_shares_a_line_with_a_variable():
    """`KEY=value  # note` is read as part of the value by Docker Compose's
    env_file, systemd and others, so the example file (which people copy to
    .env and uncomment) and the README's snippets keep comments on their own lines."""
    import re
    inline = re.compile(r"^\s*(#\s*)?(export\s+)?[A-Z_][A-Z0-9_]*=\S*\s+#")
    assert [ln for ln in EXAMPLE.splitlines() if inline.match(ln)] == []
    readme = (PROJECT_ROOT / "README.md").read_text()
    assert [ln for ln in readme.splitlines() if re.match(r"^\s*(export\s+)?ESAM_[A-Z0-9_]+=\S*\s+#", ln)] == []
    # every line that is not a comment or blank is exactly KEY=value, with no space in the value
    for ln in EXAMPLE.splitlines():
        if ln.strip() and not ln.lstrip().startswith("#"):
            assert re.fullmatch(r"[A-Z_][A-Z0-9_]*=\S*", ln), ln
