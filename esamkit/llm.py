"""
Model endpoints: which API to call, and where to go next when a call fails.

A model is reached through a ROUTE: an endpoint URL, a model name, a key and
the API spoken there:

    chat        POST <base_url>/chat/completions    (the default)
    responses   POST <base_url>/responses           (OpenAI's Responses API)
    auto        chat first, then responses on the same endpoint

A role (the crew's reasoning model, the vision model that reads diagrams, the
chat model of lesson 05) has a PRIMARY route and any number of FALLBACK
routes, tried in order when a call fails. A fallback states only what differs
from the primary: `api: responses` alone means "same endpoint, same model,
same key, the other API"; a `base_url` alone means "the same model somewhere
else".

Where routes come from:
  * a profile (config/environments/<name>.yaml, llm.roles.<role>.api and
    .fallbacks), resolved by esamkit.environment.role_routes();
  * or the environment (ESAM_REASONING_API, ESAM_REASONING_FALLBACK_1_BASE_URL, ...),
    read by esamkit.config and turned into routes by chat_routes() below.

What counts as "failed" (should_failover): no answer at all (connection
refused, timeout, TLS), an endpoint that does not serve this API or model
(404, 405), an overloaded or broken one (408, 429, 5xx), a rejected key (401,
403: the next route may have another), and a 400 that says the API or model
is not supported. A request the next endpoint would refuse too (context too
long, content filtered, a malformed request) is NOT failed over: it is raised
as it is.

A route that failed is skipped for a while (the cooldown, 5 minutes by
default), so a run does not pay for the dead endpoint on every call; when
every route is cooling down they are all tried again in order.

Everything here is the standard library: no SDK is needed to call a model.
The crew's adapter over the same policy is crew/llm.py.
"""

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import Settings, settings as default_settings

_API_NAMES = {
    "": "chat", "chat": "chat", "completions": "chat", "chat_completions": "chat", "chat.completions": "chat",
    "chat/completions": "chat",
    "responses": "responses", "response": "responses",
    "auto": "auto",
}


def normalize_api(value: str | None) -> str:
    key = (value or "").strip().lower().replace("-", "_")
    if key not in _API_NAMES:
        raise ValueError(f"unknown model API {value!r}: use chat, responses or auto")
    return _API_NAMES[key]


@dataclass(frozen=True)
class Route:
    base_url: str
    model: str
    api_key: str = ""
    api: str = "chat"                 # chat | responses
    name: str = ""

    def describe(self) -> dict:
        """What may be logged or shown: never the key."""
        return {"name": self.name, "api": self.api, "model": self.model, "base_url": self.base_url,
                "api_key_set": bool(self.api_key)}


def build_routes(primary: dict, fallbacks: list[dict] | tuple = ()) -> list[Route]:
    """Routes in the order they are tried. `primary` and each fallback are
    {base_url, model, api_key, api}; a fallback inherits whatever it leaves
    out. `api: auto` becomes two routes (chat, then responses). A route with
    no URL or no model is not configured and is left out; duplicates are
    dropped."""
    out: list[Route] = []
    seen: set[tuple] = set()

    def add(spec: dict) -> None:
        url, model = (spec.get("base_url") or "").rstrip("/"), spec.get("model") or ""
        if not (url and model):
            return
        api = normalize_api(spec.get("api"))
        for a in (("chat", "responses") if api == "auto" else (api,)):
            key = (url, model, spec.get("api_key") or "", a)
            if key in seen:
                continue
            seen.add(key)
            host = urlparse(url).netloc or url
            out.append(Route(url, model, spec.get("api_key") or "", a, f"{len(out) + 1}:{a}:{model}@{host}"))

    add(primary)
    for fb in fallbacks or ():
        add({**primary, **{k: v for k, v in fb.items() if v}})
    return out


def chat_routes(s: Settings = default_settings) -> list[Route]:
    return build_routes({"base_url": s.chat_base_url, "model": s.chat_model, "api_key": s.chat_api_key,
                         "api": s.chat_api}, [dict(f) for f in s.chat_fallbacks])


def coding_routes(s: Settings = default_settings) -> list[Route]:
    """The model approved for writing code: its own endpoint, key, API and
    fallbacks (ESAM_CODING_*, or the profile's coding role). Nothing is taken
    from the reasoning model; not configured means an empty list."""
    if not s.coding_model:
        return []
    return build_routes({"base_url": s.coding_base_url, "model": s.coding_model, "api_key": s.coding_api_key,
                         "api": s.coding_api}, [dict(f) for f in s.coding_fallbacks])


def vision_routes(s: Settings = default_settings) -> list[Route]:
    """The model that reads pictures. Not configured (no ESAM_VISION_MODEL
    and no vision role in the profile) means no picture is ever sent."""
    if not s.vision_model:
        return []
    return build_routes({"base_url": s.vision_base_url or s.chat_base_url, "model": s.vision_model,
                         "api_key": s.vision_api_key or s.chat_api_key, "api": s.vision_api or s.chat_api},
                        [dict(f) for f in s.vision_fallbacks])


# ── Errors and the failover decision ─────────────────────────────────────────

class EndpointError(RuntimeError):
    """A call to a model endpoint failed. `status` is the HTTP status, or
    None when there was no usable answer (connection, timeout, not JSON)."""

    def __init__(self, url: str, status: int | None, detail: str):
        super().__init__(f"{url}: {'HTTP ' + str(status) if status else 'no usable answer'}: {detail}")
        self.url, self.status, self.detail = url, status, detail


_SAME_EVERYWHERE = ("context_length", "context length", "maximum context", "context window", "too many tokens",
                    "content_policy", "content policy", "content_filter", "content filter", "safety system")
_NOT_SERVED_HERE = ("not supported", "unsupported", "not a chat model", "v1/responses", "v1/chat/completions",
                    "unknown url", "unrecognized request url", "invalid url", "model_not_found", "does not exist",
                    "no such model", "unknown model", "not found", "not implemented", "not available")
_RETRY_ELSEWHERE = {401, 403, 404, 405, 408, 409, 425, 429}


def _status(e: BaseException) -> int | None:
    for value in (getattr(e, "status_code", None), getattr(e, "status", None),
                  getattr(getattr(e, "response", None), "status_code", None)):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def should_failover(exc: BaseException) -> bool:
    """Is it worth trying the next route? Works on this module's errors, on
    the OpenAI SDK's (status_code, APIConnectionError, APITimeoutError) and on
    whatever wraps them (`raise ... from e`)."""
    chain, e = [], exc
    while e is not None and len(chain) < 8:
        chain.append(e)
        e = e.__cause__ or e.__context__
    text = " ".join(str(x) for x in chain).lower()
    if any(p in text for p in _SAME_EVERYWHERE):
        return False
    for x in chain:
        status = _status(x)
        if status is not None:
            if status in _RETRY_ELSEWHERE or status >= 500:
                return True
            if status in (400, 422):
                return any(p in text for p in _NOT_SERVED_HERE)
            return False
    for x in chain:
        name = type(x).__name__
        if isinstance(x, EndpointError):                 # status None: no usable answer
            return True
        if isinstance(x, (ConnectionError, TimeoutError)) or "Connection" in name or "Timeout" in name:
            return True
    return False


class Failover:
    """Try routes in order; remember which one is down and which one served.

    `run(attempt)` calls attempt(route) and returns its result. An error that
    should_failover() accepts moves on to the next route; any other error is
    raised unchanged. When every route fails, the first route's error is
    raised, so a caller sees the error it would have seen without this class.
    """

    def __init__(self, routes: list[Route], cooldown: float = 300.0, clock=time.monotonic, log=None):
        if not routes:
            raise ValueError("no model endpoint is configured")
        self.routes, self.cooldown, self._clock, self._log = list(routes), cooldown, clock, log
        self._down_until: dict[str, float] = {}
        self.served: dict[str, int] = {r.name: 0 for r in routes}
        self.failures: list[dict] = []

    def order(self) -> list[Route]:
        now = self._clock()
        up = [r for r in self.routes if self._down_until.get(r.name, 0) <= now]
        return up + [r for r in self.routes if r not in up]

    def _failed(self, route: Route, e: BaseException) -> None:
        self._down_until[route.name] = self._clock() + self.cooldown
        self.failures.append({"route": route.name, "error": f"{type(e).__name__}: {str(e)[:300]}"})
        if self._log:
            self._log(f"model endpoint {route.name} failed ({type(e).__name__}: {str(e)[:160]}); trying the next one")

    def _served(self, route: Route) -> None:
        self.served[route.name] += 1
        self._down_until.pop(route.name, None)

    def _give_up(self, errors: list[tuple[Route, BaseException]]):
        """Every route failed: raise the PRIMARY route's own error, so the
        caller sees what it would have seen without fallbacks (callers such as
        CrewAI decide by the error's type whether to retry), with the other
        routes' failures attached as `failover_errors` and as notes."""
        first = errors[0][1]
        first.failover_errors = [(r.name, f"{type(e).__name__}: {str(e)[:300]}") for r, e in errors]
        if len(errors) > 1 and hasattr(first, "add_note"):
            for route, e in errors[1:]:
                first.add_note(f"fallback {route.name} also failed: {type(e).__name__}: {str(e)[:300]}")
        raise first

    def run(self, attempt):
        errors: list[tuple[Route, BaseException]] = []
        for route in self.order():
            try:
                result = attempt(route)
            except Exception as e:
                if not should_failover(e):
                    raise
                self._failed(route, e)
                errors.append((route, e))
                continue
            self._served(route)
            return result
        self._give_up(errors)

    async def arun(self, attempt):
        errors: list[tuple[Route, BaseException]] = []
        for route in self.order():
            try:
                result = await attempt(route)
            except Exception as e:
                if not should_failover(e):
                    raise
                self._failed(route, e)
                errors.append((route, e))
                continue
            self._served(route)
            return result
        self._give_up(errors)

    def summary(self) -> dict:
        """For the usage ledger: calls per route, and what failed."""
        return {"served": {k: v for k, v in self.served.items() if v}, "failures": self.failures,
                "used_fallback": any(v for k, v in self.served.items() if k != self.routes[0].name)}


# ── HTTP ─────────────────────────────────────────────────────────────────────

# Sent with every request. Without it Python announces itself as
# "Python-urllib/3.x", which some gateways and CDNs in front of model APIs
# refuse (HTTP 403) before the request reaches the API.
USER_AGENT = "ESAM/1.0"


def post_json(url: str, payload: dict, api_key: str = "", timeout: float = 120.0) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise EndpointError(url, e.code, e.read().decode("utf-8", "replace")[:2000].strip() or e.reason) from None
    except (urllib.error.URLError, OSError) as e:        # refused, DNS, TLS, timeout
        raise EndpointError(url, None, f"{type(e).__name__}: {getattr(e, 'reason', e)}") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise EndpointError(url, None, f"the answer is not JSON: {body[:200]!r}") from None
    if not isinstance(data, dict):
        raise EndpointError(url, None, "the answer is not a JSON object")
    return data


def data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _text_parts(content) -> str:
    """Text of a message whose content is a string or a list of parts."""
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict)
                   and p.get("type") in ("text", "output_text"))


def request_text(route: Route, system: str, user: str, images: list[tuple[str, bytes]] | tuple = (),
                 timeout: float = 120.0) -> tuple[str, dict]:
    """One call on one route. Returns (text, usage). `images` are
    (mime type, bytes) pairs sent with the user message."""
    if route.api == "responses":
        content = [{"type": "input_text", "text": user}]
        content += [{"type": "input_image", "image_url": data_url(m, b)} for m, b in images]
        payload: dict = {"model": route.model, "input": [{"role": "user", "content": content}]}
        if system:
            payload["instructions"] = system
        url = f"{route.base_url}/responses"
        data = post_json(url, payload, route.api_key, timeout)
        text = "".join(_text_parts(item.get("content")) for item in data.get("output") or []
                       if isinstance(item, dict) and item.get("type") == "message") or data.get("output_text") or ""
    else:
        user_content = user if not images else (
            [{"type": "text", "text": user}] + [{"type": "image_url", "image_url": {"url": data_url(m, b)}}
                                                for m, b in images])
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user_content}]
        url = f"{route.base_url}/chat/completions"
        data = post_json(url, {"model": route.model, "messages": messages}, route.api_key, timeout)
        choices = data.get("choices") or [{}]
        text = _text_parts((choices[0].get("message") or {}).get("content"))
    if not text.strip():
        raise EndpointError(url, None, f"the answer holds no text: {json.dumps(data)[:300]}")
    return text, data.get("usage") or {}


_CHAINS: dict[tuple, Failover] = {}


def failover_for(routes: list[Route], s: Settings = default_settings) -> Failover:
    """One Failover per set of routes for the life of the process, so a route
    found down stays skipped across calls."""
    key = tuple(routes)
    if key not in _CHAINS:
        _CHAINS[key] = Failover(routes, cooldown=s.llm_failover_cooldown)
    return _CHAINS[key]


def complete(system: str, user: str, images: list[tuple[str, bytes]] | tuple = (),
             routes: list[Route] | None = None, s: Settings = default_settings) -> str:
    """Ask the chat model (or the given routes), failing over between routes."""
    routes = chat_routes(s) if routes is None else routes
    if not routes:
        raise ValueError("no chat model is configured: set ESAM_REASONING_BASE_URL and ESAM_REASONING_MODEL")
    return failover_for(routes, s).run(lambda r: request_text(r, system, user, images, s.llm_timeout)[0])
