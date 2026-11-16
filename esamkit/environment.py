"""
Step 0: what does THIS environment offer?

The same ESAM code runs on a learning laptop, on the IL5 pilot (gateway
models, VM-1 PostgreSQL, no internet) and later with JanusGraph. A profile
(config/environments/<name>.yaml) DECLARES what a place should have; the
probes here CHECK it, and `capabilities()` turns the result into the list of
tools the crew may use. A tool whose service is down is never handed to an
agent, so the crew says "the graph is unavailable" instead of failing halfway.

    python -m esamkit.environment                 # profile from ESAM_ENV (default: local)
    python -m esamkit.environment --env pilot-il5 --no-network

Secrets are named, never stored: a profile gives the NAME of an environment
variable (or a line in esam/.env) and probes report only "set" / "missing".
Probes never load the embedding model and never spend chat tokens: an LLM
endpoint is checked by listing its models (every fallback endpoint too), and
an embeddings endpoint by embedding two words, which also proves the model
name and tells the vector size.

Model endpoints in a profile (the rules are in esamkit/llm.py):

    llm.roles.<role>.api         chat (default) | responses | auto
    llm.roles.<role>.fallbacks   endpoints tried in order when a call fails;
                                 each states only what differs from the primary
    embeddings.model / .base_url the embedding model and where it runs
"""

import argparse
import json
import os
import platform
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .config import PROJECT_ROOT, Settings, _fallbacks as env_fallbacks, default_model_dir, env as config_env
from .config import settings as default_settings
from .llm import Route, build_routes, normalize_api

PROFILE_DIR = PROJECT_ROOT / "config" / "environments"
OK = ("ok", "warning")

# Which services each crew tool needs. A tool is enabled only if all are OK.
TOOL_NEEDS = {
    "ontology_lookup": ["files"],
    "ontology_check": ["files"],
    "review_document": ["files"],
    "search_documents": ["vector", "embeddings"],
    "walk_graph": ["graph"],
    "check_coverage": ["graph"],
    "verified_by": ["graph"],
    "health_report": ["graph", "database"],
    "ingest_document": ["vector", "embeddings", "graph", "database"],
    "verify_ingestion": ["vector", "embeddings", "graph", "database"],
    "read_brief": ["files"],
    "read_source": ["files"],
    "standard_clause": ["files"],
    "search_standards": ["files"],
    "save_draft": ["files", "graph"],
    "check_draft": ["files", "graph"],
    "build_evidence_pack": ["files", "graph"],
}


@dataclass
class Probe:
    name: str
    status: str          # ok | warning | unavailable | not_configured | not_implemented
    detail: str
    data: dict = field(default_factory=dict)


# ── Profiles ────────────────────────────────────────────────────────────────

def profile_names() -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))


def load_profile(name: str | None = None) -> dict:
    name = name or os.getenv("ESAM_ENV", "local")
    path = PROFILE_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no environment profile {name!r}; have {profile_names()}")
    return yaml.safe_load(path.read_text())


def _env(name: str | None) -> str:
    """A variable named by a profile, by its current or an older name (esamkit.config.env)."""
    return config_env(name)


def _pick(spec: dict, key: str):
    """A profile value that .env may set: `<key>_env` names a variable which,
    when set, is used; otherwise the value written under `<key>`.

        model: gpt-5.1
        model_env: ESAM_REASONING_MODEL      # set it in .env and it replaces gpt-5.1
    """
    return _env(spec.get(f"{key}_env")) or spec.get(key) or ""


def mask_dsn(dsn: str) -> str:
    """postgresql://user:secret@host/db -> postgresql://user:***@host/db"""
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", dsn)


ROLE_ALIASES = {"coding": ("coding", "code")}       # `code` was the role's first name


def role_spec(profile: dict, role: str) -> dict | None:
    """The profile's entry for a role, as written, or None when the role has
    no model. `coding` and `vision` are optional roles: with no model (none
    written, and the variable `model_env` names not set) they do not exist."""
    roles = (profile.get("llm") or {}).get("roles") or {}
    spec = next((roles[k] for k in ROLE_ALIASES.get(role, (role,)) if roles.get(k)), None)
    if not spec or (role != "reasoning" and not _pick(spec, "model")):
        return None
    return spec


def _concrete(spec: dict) -> dict:
    return {"base_url": _pick(spec, "base_url"), "model": _pick(spec, "model"),
            "api_key": _env(spec.get("api_key_env")), "api": _pick(spec, "api")}


def _fallback_specs(spec: dict) -> tuple[list[dict], list[str]]:
    """(usable fallbacks as concrete values, why the others are skipped).
    Fallbacks come from the profile's `fallbacks:` list and from the numbered
    variables under `fallbacks_env` (ESAM_REASONING_FALLBACK -> _1_BASE_URL,
    _1_API_KEY, _1_MODEL, _1_API, then _2_ ...). A fallback that names a
    variable which is not set is not configured: it is skipped, never quietly
    pointed at the primary endpoint."""
    usable, skipped = [], []
    for i, fb in enumerate(spec.get("fallbacks") or [], 1):
        missing = [fb[k] for k in ("base_url_env", "api_key_env") if fb.get(k) and not _env(fb[k]) and not fb.get(k[:-4])]
        if missing:
            skipped.append(f"fallback {i}: set {' and '.join(missing)}")
        else:
            usable.append(_concrete(fb))
    prefix = spec.get("fallbacks_env")
    if prefix:
        usable += [dict(f) for f in env_fallbacks(prefix.removesuffix("_FALLBACK"))]
    return usable, skipped


def role_endpoint(profile: dict, role: str) -> tuple[dict, list[dict], list[str]] | None:
    """(primary, fallbacks, skipped fallbacks) for a role, as concrete values.
    Reasoning and coding are separate: neither takes anything from the other.
    Only the vision role borrows: whatever it leaves unset (URL, key, API,
    fallbacks) comes from the reasoning role, so it may state just a model."""
    spec = role_spec(profile, role)
    if not spec:
        return None
    primary = _concrete(spec)
    fallbacks, skipped = _fallback_specs(spec)
    if role == "vision":
        got = role_endpoint(profile, "reasoning")
        if got:
            own_endpoint = bool(primary["base_url"])
            for key in ("base_url", "api_key", "api"):
                primary[key] = primary[key] or got[0][key]
            if not own_endpoint and not fallbacks and not spec.get("fallbacks"):
                fallbacks, skipped = got[1], got[2]
    return primary, fallbacks, skipped


def role_routes(profile: dict, role: str) -> list[Route]:
    """The endpoints a role's calls go to, in order: its primary, then its
    fallbacks (esamkit/llm.py). Empty when the role is not configured."""
    got = role_endpoint(profile, role)
    return build_routes(got[0], got[1]) if got else []


def resolve(profile: dict) -> dict:
    """Concrete values for this profile: URLs, keys (presence only), DSN."""
    roles = {}
    for name in ((profile.get("llm") or {}).get("roles") or {}):
        role = next((r for r, names in ROLE_ALIASES.items() if name in names), name)
        got, spec = role_endpoint(profile, role), role_spec(profile, role)
        if not got:
            roles[role] = None
            continue
        primary, _, skipped = got
        roles[role] = {"model": primary["model"], "model_env": spec.get("model_env"),
                       "base_url": primary["base_url"],
                       "base_url_env": spec.get("base_url_env"),
                       "api_key_env": spec.get("api_key_env"),
                       "api_key_set": bool(primary["api_key"]),
                       "api": normalize_api(primary["api"]),
                       "routes": [r.describe() for r in role_routes(profile, role)],
                       "fallbacks_skipped": skipped}
    db = profile.get("database") or {}
    dsn = _env(db.get("dsn_env")) or db.get("dsn_default", "")
    graph = profile.get("graph") or {}
    return {"roles": roles, "dsn": dsn, "dsn_masked": mask_dsn(dsn) if dsn else "",
            "dsn_source": ("env " + db.get("dsn_env", "")) if _env(db.get("dsn_env")) else
                          ("profile default (learning only)" if dsn else "none"),
            "graph_url": _pick(graph, "url")}


def embedding_choice(profile: dict, base: Settings = default_settings) -> dict:
    """Which embedding model this profile uses and where it runs.

        embeddings:
          backend: auto | local | openai | none   # auto: an endpoint if a URL is set, else local
          model: BAAI/bge-base-en-v1.5            # (model_id is the older name)
          model_env: ESAM_EMBED_MODEL             # a variable that overrides `model` when set
          base_url: https://host/v1               # or base_url_env: ESAM_EMBED_BASE_URL
          api_key_env: ESAM_EMBED_API_KEY
          model_dir: models/bge-base-en-v1.5      # local backend; default models/<model name>
          dimensions: 768                         # endpoint backend, optional
    """
    emb = profile.get("embeddings") or {}
    url = _pick(emb, "base_url")
    backend = emb.get("backend", base.embed_backend)
    if backend == "auto":                                     # ESAM_EMBED_BACKEND, else by whether there is a URL
        backend = os.getenv("ESAM_EMBED_BACKEND") or ("openai" if url else "local")
    # The model: the variable the profile names, when set. An endpoint set up under the older names
    # (ESAM_OPENAI_*) has its model in ESAM_OPENAI_EMBED_MODEL: that, not the local model the profile writes.
    old_name = os.getenv("ESAM_OPENAI_EMBED_MODEL", "") if backend == "openai" and emb.get("model_env") else ""
    model = _env(emb.get("model_env")) or old_name or emb.get("model") or emb.get("model_id") or ""
    model_dir = Path(_pick(emb, "model_dir") or (default_model_dir(model) if model else base.embed_model_dir))
    return {"backend": backend, "model": model, "base_url": url,
            "model_source": ("env " + emb["model_env"]) if _env(emb.get("model_env")) else
                            "env ESAM_OPENAI_EMBED_MODEL" if old_name else "profile",
            "url_source": (("env " + emb["base_url_env"]) if _env(emb.get("base_url_env")) else "profile") if url else "none",
            "base_url_env": emb.get("base_url_env"), "api_key_env": emb.get("api_key_env"),
            "api_key": _env(emb.get("api_key_env")),
            "model_dir": model_dir if model_dir.is_absolute() else PROJECT_ROOT / model_dir,
            "dimensions": int(emb.get("dimensions") or 0)}


def _fallback_items(fallbacks: list[dict]) -> tuple:
    return tuple(tuple(sorted((k, v) for k, v in fb.items() if v)) for fb in fallbacks)


def settings_for(profile: dict, base: Settings = default_settings) -> Settings:
    """esamkit Settings for this profile, so factories build the right backends."""
    r = resolve(profile)
    reasoning, coding, vision = (role_endpoint(profile, role) for role in ("reasoning", "coding", "vision"))
    emb, vec, graph = embedding_choice(profile, base), profile.get("vector") or {}, profile.get("graph") or {}
    remote = emb["backend"] == "openai"
    out = replace(
        base,
        embed_backend=emb["backend"],
        embed_model_id=emb["model"] or base.embed_model_id,
        embed_model_dir=emb["model_dir"],
        embed_base_url=emb["base_url"] or (base.embed_base_url if remote else ""),
        embed_api_key=emb["api_key"] or base.embed_api_key,
        embed_model=(emb["model"] or base.embed_model) if remote else base.embed_model,
        embed_dimensions=emb["dimensions"] or base.embed_dimensions,
        vector_backend=vec.get("backend", base.vector_backend),
        collection=_pick(vec, "collection") or base.collection,
        pg_dsn=r["dsn"] or base.pg_dsn,
        graph_backend=graph.get("backend", base.graph_backend),
        diagrams=_pick(profile.get("documents") or {}, "diagrams") or base.diagrams,
    )
    if reasoning:
        p = reasoning[0]
        out = replace(out, chat_base_url=p["base_url"], chat_api_key=p["api_key"] or base.chat_api_key,
                      chat_model=p["model"] or base.chat_model, chat_api=p["api"] or base.chat_api,
                      chat_fallbacks=_fallback_items(reasoning[1]))
    # The profile decides which optional roles exist: one it does not configure is off, whatever the base says.
    p, fallbacks = (coding[0], coding[1]) if coding else ({}, [])
    out = replace(out, coding_model=p.get("model", ""), coding_base_url=p.get("base_url", ""),
                  coding_api_key=p.get("api_key", ""), coding_api=p.get("api") or "chat",
                  coding_fallbacks=_fallback_items(fallbacks))
    if vision:
        p = vision[0]
        out = replace(out, vision_model=p["model"], vision_base_url=p["base_url"], vision_api_key=p["api_key"],
                      vision_api=p["api"], vision_fallbacks=_fallback_items(vision[1]))
    return out


def apply_telemetry(profile: dict) -> None:
    """Turn CrewAI / OpenTelemetry phone-home off unless the profile says otherwise."""
    if (profile.get("telemetry") or {}).get("crewai", "off") == "off":
        os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
        os.environ["CREWAI_TRACING_ENABLED"] = "false"
        os.environ["OTEL_SDK_DISABLED"] = "true"


# ── Probes ──────────────────────────────────────────────────────────────────

def probe_host() -> Probe:
    cpu, flags, mem_gb = platform.processor() or "unknown", set(), 0.0
    try:
        info = Path("/proc/cpuinfo").read_text()
        cpu = re.search(r"model name\s*:\s*(.+)", info).group(1)
        flags = set(re.search(r"flags\s*:\s*(.+)", info).group(1).split())
        mem_gb = int(re.search(r"MemTotal:\s*(\d+)", Path("/proc/meminfo").read_text()).group(1)) / 1024 / 1024
    except (OSError, AttributeError):
        pass
    data = {"cpu": cpu, "cores": os.cpu_count(), "ram_gb": round(mem_gb, 1), "avx2": "avx2" in flags,
            "python": platform.python_version()}
    detail = f"{cpu}, {os.cpu_count()} cores, {data['ram_gb']} GB RAM, Python {data['python']}"
    if flags and "avx2" not in flags:
        return Probe("host", "warning", detail + ". No AVX2: local embedding can crash intermittently "
                                                 "(SIGILL); re-run if it does", data)
    return Probe("host", "ok", detail, data)


def probe_files(s: Settings) -> Probe:
    from .ontology import check_baseline, load_ontology

    missing = [str(p.relative_to(PROJECT_ROOT)) for p in (s.baseline_yaml, s.ontology_yaml, s.documents_dir)
               if not p.exists()]
    if missing:
        return Probe("files", "unavailable", "missing: " + ", ".join(missing))
    onto = load_ontology(s.ontology_yaml)
    findings = onto.check_self() + check_baseline(onto, yaml.safe_load(s.baseline_yaml.read_text()), s.documents_dir)
    errors = sum(f.level == "error" for f in findings)
    from .readers import document_files
    docs = len(document_files(s.documents_dir))
    try:
        from .standards import Library
        lib = Library()
        held = {sid: len(lib.clauses(sid)) for sid, v in lib.standards.items() if v.get("text")}
        standards = ", ".join(f"{k} {n} clauses" for k, n in held.items())
    except (OSError, KeyError, ValueError) as e:
        held, standards = {}, f"standards library unreadable ({type(e).__name__})"
    data = {"ontology_errors": errors, "ontology_warnings": len(findings) - errors, "documents": docs, "standards": held}
    status = "ok" if not errors and held and all(held.values()) else "warning"
    return Probe("files", status, f"baseline, ontology v{onto.data.get('metadata', {}).get('version', '?')}, "
                                  f"{docs} documents; baseline vs ontology: {errors} errors, "
                                  f"{len(findings) - errors} warnings; standards: {standards}", data)


def probe_database(profile: dict, r: dict) -> Probe:
    if (profile.get("database") or {}).get("backend", "none") == "none":
        return Probe("database", "not_configured", "profile has no database")
    if not r["dsn"]:
        return Probe("database", "not_configured",
                     f"set {(profile.get('database') or {}).get('dsn_env')} to the PostgreSQL connection string")
    try:
        import psycopg

        t = time.perf_counter()
        with psycopg.connect(r["dsn"], connect_timeout=3) as conn:
            version = conn.execute("SHOW server_version").fetchone()[0]
            vec = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        ms = round((time.perf_counter() - t) * 1000)
    except Exception as e:  # report every failure the same way: unavailable, with the reason
        return Probe("database", "unavailable", f"{r['dsn_masked']}: {type(e).__name__}: {str(e).strip()[:160]}")
    data = {"server_version": version, "pgvector": vec[0] if vec else None, "connect_ms": ms,
            "dsn": r["dsn_masked"], "dsn_source": r["dsn_source"]}
    return Probe("database", "ok", f"PostgreSQL {version}, pgvector {data['pgvector'] or 'not installed'}, "
                                   f"{ms} ms, {r['dsn_masked']} ({r['dsn_source']})", data)


def probe_vector(profile: dict, r: dict, db: Probe, s: Settings | None = None) -> Probe:
    from .factory import embedder_identity

    vec = profile.get("vector") or {}
    expected = embedder_identity(s or settings_for(profile))
    if vec.get("backend", "none") == "none":
        return Probe("vector", "not_configured", "profile has no vector store: document search is off")
    if vec["backend"] != "pgvector":
        return Probe("vector", "not_implemented", f"vector backend {vec['backend']!r} has no store in this build")
    if db.status not in OK:
        return Probe("vector", "unavailable", f"needs the database ({db.status.replace('_', ' ')})")
    import psycopg

    from .vectorstores.pgvector_store import chunk_counts

    counts = chunk_counts(r["dsn"], vec.get("collection", "esam_documents"))
    with psycopg.connect(r["dsn"], connect_timeout=3) as conn:
        has = conn.execute("SELECT to_regclass('esam_collections')").fetchone()[0]
        stored = conn.execute("SELECT embed_model_id FROM esam_collections WHERE name = %s",
                              (vec.get("collection", "esam_documents"),)).fetchone() if has else None
    data = {"collection": vec.get("collection"), "chunks": sum(counts.values()), "documents": len(counts),
            "stored_model": stored[0] if stored else None}
    if stored and stored[0] != expected:
        return Probe("vector", "unavailable", f"collection was embedded with {stored[0]} but the profile uses "
                                              f"{expected}: re-ingest or change the profile", data)
    if not counts:
        return Probe("vector", "warning", "collection is empty: run the ingest", data)
    return Probe("vector", "ok", f"{data['chunks']} chunks from {data['documents']} documents, "
                                 f"embedded with {data['stored_model']}", data)


def probe_graph(profile: dict, r: dict, db: Probe) -> Probe:
    backend = (profile.get("graph") or {}).get("backend", "none")
    if backend == "none":
        return Probe("graph", "not_configured", "profile has no graph: graph, coverage and health tools are off")
    if backend == "janusgraph":
        url = r["graph_url"]
        if not url:
            return Probe("graph", "not_implemented", "JanusGraph declared; no URL set and no JanusGraph store in this build")
        u = urlparse(url)
        try:
            socket.create_connection((u.hostname, u.port or 8182), timeout=3).close()
            reach = "reachable"
        except OSError as e:
            reach = f"not reachable ({e.__class__.__name__})"
        return Probe("graph", "not_implemented", f"JanusGraph at {url} is {reach}, but this build has no "
                                                 "JanusGraph graph store yet", {"url": url})
    if backend != "postgres":
        return Probe("graph", "not_implemented", f"graph backend {backend!r} unknown")
    if db.status not in OK:
        return Probe("graph", "unavailable", f"needs the database ({db.status.replace('_', ' ')})")
    import psycopg

    with psycopg.connect(r["dsn"], connect_timeout=3) as conn:
        if conn.execute("SELECT to_regclass('graph_vertices')").fetchone()[0] is None:
            return Probe("graph", "warning", "graph tables not created yet: run the graph load")
        rows = dict(conn.execute("SELECT label, count(*) FROM graph_vertices GROUP BY label").fetchall())
        edges = conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0]
    data = {"vertices": sum(rows.values()), "edges": edges, "by_label": rows}
    if not rows:
        return Probe("graph", "warning", "graph is empty: run the graph load", data)
    return Probe("graph", "ok", f"PostgreSQL recursive CTEs: {data['vertices']} vertices, {edges} edges "
                                f"({rows.get('Requirement', 0)} requirements, {rows.get('TestCase', 0)} test cases)", data)


def _is_public(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return not (host.endswith(".local") or host.startswith(("10.", "192.168.", "127.")) or host == "localhost")


def probe_embeddings(profile: dict, s: Settings, network: bool = True) -> Probe:
    choice = embedding_choice(profile)
    backend, model = choice["backend"], choice["model"] or s.embed_model_id
    if backend == "none":
        return Probe("embeddings", "not_configured", "profile has no embedding model: document search is off")
    if backend == "openai":
        url, name = s.embed_base_url, s.embed_model
        data = {"backend": "openai", "model": name, "base_url": url, "model_source": choice["model_source"],
                "url_source": choice["url_source"]}
        if not url:
            return Probe("embeddings", "not_configured",
                         f"set {choice['base_url_env'] or 'embeddings.base_url'} to the embeddings endpoint URL "
                         "(https://<host>/v1)", data)
        if not name:
            return Probe("embeddings", "not_configured", f"endpoint {url}: say which model with embeddings.model "
                                                         "(or ESAM_EMBED_MODEL)", data)
        where = f"{name} at {url}/embeddings (model from {choice['model_source']}, URL from {choice['url_source']})"
        if not network:
            return Probe("embeddings", "warning", f"{where}; not contacted (--no-network)", data)
        if _is_public(url) and not (profile.get("network") or {}).get("internet", False):
            return Probe("embeddings", "unavailable", f"{url} is a public host but the profile is air-gapped", data)
        try:
            from .factory import get_embedder

            t = time.perf_counter()
            dim = get_embedder(replace(s, llm_timeout=min(s.llm_timeout, 15))).dim
        except Exception as e:
            return Probe("embeddings", "unavailable", f"{where}: {type(e).__name__}: {str(e)[:600]}", data)
        data.update(dim=dim, latency_ms=round((time.perf_counter() - t) * 1000))
        return Probe("embeddings", "ok", f"{where}, {dim}-dim, {data['latency_ms']} ms", data)
    cfg = s.embed_model_dir / "config.json"
    if not cfg.exists():
        return Probe("embeddings", "unavailable", f"{model} is not staged in {s.embed_model_dir}: run "
                     "lessons/00_setup/stage_model.py, or set embeddings.base_url (ESAM_EMBED_BASE_URL) to use an endpoint")
    size = sum(f.stat().st_size for f in s.embed_model_dir.rglob("*") if f.is_file()) / 1e6
    dim = json.loads(cfg.read_text()).get("hidden_size")
    return Probe("embeddings", "ok", f"{model} staged locally in {s.embed_model_dir.name}/ ({size:.0f} MB, {dim}-dim, "
                                     f"CPU, offline; model from {choice['model_source']})",
                 {"backend": "local", "model_id": model, "dim": dim, "mb": round(size)})


def _endpoint_state(profile: dict, base_url: str, model: str, key: str, key_name: str, network: bool) -> Probe:
    """One model endpoint: reachable, and offering the model (GET <base_url>/models)."""
    if not network:
        return Probe("", "warning", f"{model} at {base_url}: key set; not contacted (--no-network)", {"model": model})
    if _is_public(base_url) and not (profile.get("network") or {}).get("internet", False):
        return Probe("", "unavailable", f"{base_url} is a public host but the profile is air-gapped")
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            models = [m.get("id") for m in json.loads(resp.read()).get("data", [])]
    except urllib.error.HTTPError as e:
        return Probe("", "unavailable", f"{base_url}: HTTP {e.code} (check {key_name})")
    except Exception as e:
        return Probe("", "unavailable", f"{base_url}: {type(e).__name__}: {str(e)[:120]}")
    ms = round((time.perf_counter() - t) * 1000)
    data = {"model": model, "endpoint": base_url, "models_listed": len(models), "latency_ms": ms}
    if models and model not in models:
        return Probe("", "unavailable", f"{model} is not offered at {base_url} ({len(models)} models listed)", data)
    return Probe("", "ok", f"{model} at {base_url}, key from {key_name}, {ms} ms", data)


def probe_llm(profile: dict, r: dict, role: str, network: bool) -> Probe:
    """The role's primary endpoint, then its fallbacks. The role is usable
    (ok or warning) if any of its endpoints is: 'warning' when calls will
    start on a fallback because the primary is down or not configured."""
    spec = r["roles"].get(role)
    name = f"llm.{role}"
    if spec is None:
        raw = next((x for k in ROLE_ALIASES.get(role, (role,))
                    if (x := ((profile.get("llm") or {}).get("roles") or {}).get(k))), None) or {}
        hint = f": set {raw['model_env']} to turn it on" if raw.get("model_env") else ""
        return Probe(name, "not_configured", "no model for this role in the profile" + hint)
    if not spec["base_url"]:
        primary = Probe(name, "not_configured", f"set {spec['base_url_env']} to the endpoint URL")
    elif not spec["api_key_set"]:
        primary = Probe(name, "not_configured", f"{spec['model']} at {spec['base_url']}: set {spec['api_key_env']}")
    else:
        primary = replace(_endpoint_state(profile, spec["base_url"], spec["model"], _env(spec["api_key_env"]),
                                          spec["api_key_env"], network), name=name)
    routes = role_routes(profile, role) if spec.get("routes") else []
    apis = [x["api"] for x in spec.get("routes") or [] if x["base_url"] == (spec["base_url"] or "").rstrip("/")
            and x["model"] == spec["model"]]
    if primary.status in OK and apis and apis != ["chat"]:
        primary.detail += f"; API: {' then '.join(apis)}"
    primary.data["routes"] = spec.get("routes") or []
    others, seen = [], {((spec["base_url"] or "").rstrip("/"), spec["model"])}
    for route in routes:
        if (route.base_url, route.model) in seen:
            continue
        seen.add((route.base_url, route.model))
        state = _endpoint_state(profile, route.base_url, route.model, route.api_key, "its api_key_env", network)
        others.append((route, state))
    notes = [f"fallback {route.model} at {route.base_url} ({route.api}): {state.status}"
             + ("" if state.status == "ok" else f" ({state.detail})") for route, state in others]
    notes += spec.get("fallbacks_skipped") or []
    primary.data["fallbacks"] = [{"route": route.describe(), "status": state.status} for route, state in others]
    if primary.status not in OK and any(state.status in OK for _, state in others):
        return Probe(name, "warning", f"primary endpoint unusable ({primary.detail}); calls will go to a fallback. "
                     + "; ".join(notes), primary.data)
    if notes:
        primary.detail += "; " + "; ".join(notes)
    return primary


def probe_crewai(profile: dict) -> Probe:
    try:
        import importlib.metadata as md

        version = md.version("crewai")
    except Exception:
        return Probe("crewai", "unavailable", "CrewAI not installed: pip install -r requirements-crew.txt")
    tele = (profile.get("telemetry") or {})
    detail = f"CrewAI {version}; telemetry {tele.get('crewai', 'off')}, crew memory {tele.get('crew_memory', 'off')}"
    if tele.get("crew_memory", "off") != "off" and (profile.get("embeddings") or {}).get("backend") != "openai":
        return Probe("crewai", "warning", detail + ": memory needs an embeddings API this profile doesn't have",
                     {"version": version})
    return Probe("crewai", "ok", detail, {"version": version})


# ── Putting it together ─────────────────────────────────────────────────────

def inspect(profile_name: str | None = None, network: bool = True) -> dict:
    profile = load_profile(profile_name)
    r, s = resolve(profile), settings_for(load_profile(profile_name))
    db = probe_database(profile, r)
    probes = [probe_host(), probe_files(s), db, probe_vector(profile, r, db, s), probe_graph(profile, r, db),
              probe_embeddings(profile, s, network)]
    probes += [probe_llm(profile, r, role, network) for role in r["roles"]]
    probes.append(probe_crewai(profile))
    return {"profile": profile["name"], "description": profile.get("description", ""),
            "probes": [asdict(p) for p in probes], "capabilities": capabilities(probes),
            "limits": profile.get("limits") or {}, "usage_sink": (profile.get("usage") or {}).get("sink", "file")}


def capabilities(probes: list[Probe]) -> dict:
    """Which crew tools may be used, and why a tool is off."""
    state = {p.name: p.status for p in probes}
    tools = {}
    for tool, needs in TOOL_NEEDS.items():
        bad = [n for n in needs if state.get(n) not in OK]
        tools[tool] = {"enabled": not bad, "blocked_by": bad}
    return {"tools": tools, "agents_can_run": state.get("llm.reasoning") in OK,
            "enabled": [t for t, v in tools.items() if v["enabled"]]}


def main() -> None:
    ap = argparse.ArgumentParser(description="Check what this environment offers (ESAM step 0).")
    ap.add_argument("--env", default=None, help=f"profile name ({', '.join(profile_names())}); default ESAM_ENV or local")
    ap.add_argument("--no-network", action="store_true", help="don't contact model endpoints")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rep = inspect(args.env, network=not args.no_network)
    if args.json:
        print(json.dumps(rep, indent=1))
        return
    mark = {"ok": "✓", "warning": "!", "unavailable": "✗", "not_configured": "–", "not_implemented": "–"}
    print(f"Environment: {rep['profile']}  ({rep['description']})\n")
    for p in rep["probes"]:
        print(f"  {mark[p['status']]} {p['name']:<14} {p['status']:<15} {p['detail']}")
    caps = rep["capabilities"]
    print("\nTools the crew may use:")
    for t, v in caps["tools"].items():
        print(f"  {'✓' if v['enabled'] else '✗'} {t:<17}" + ("" if v["enabled"] else f" off: needs {', '.join(v['blocked_by'])}"))
    print("\nAgents can run." if caps["agents_can_run"] else "\nAgents CANNOT run: no reasoning model is available.")
    sys.exit(0 if caps["agents_can_run"] else 1)


if __name__ == "__main__":
    main()
