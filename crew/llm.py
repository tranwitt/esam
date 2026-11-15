"""
The crew's model, with fallback endpoints and the Responses API.

A role in the environment profile has a primary endpoint and, optionally,
fallbacks (config/environments/<name>.yaml; the rules are in esamkit/llm.py):

    llm:
      roles:
        reasoning:
          model: gpt-5.1
          model_env: ESAM_REASONING_MODEL            # a variable in .env that, when set, replaces the line above
          base_url_env: ESAM_REASONING_BASE_URL
          api_key_env: ESAM_REASONING_API_KEY
          api: chat                                  # chat | responses | auto
          api_env: ESAM_REASONING_API
          fallbacks_env: ESAM_REASONING_FALLBACK     # ESAM_REASONING_FALLBACK_1_API=responses, _2_BASE_URL=... in .env
        coding:                                      # a separate endpoint: nothing is shared with reasoning
          model: nemotron-4-15b
          base_url_env: ESAM_CODING_BASE_URL
          api_key_env: ESAM_CODING_API_KEY
          fallbacks_env: ESAM_CODING_FALLBACK

A profile may also list fallbacks itself (`fallbacks: [{api: responses},
{base_url_env: MY_SECOND_URL, api_key_env: MY_SECOND_KEY}]`); both are used.

With ONE route the crew gets a plain CrewAI `LLM`, exactly as before
(`api: responses` just selects CrewAI's Responses API support). With more
than one it gets a `FailoverLLM`: a thin CrewAI BaseLLM that holds one
ordinary `LLM` per route and hands every call to the first route that is up.
When a call fails in a way another endpoint could fix (esamkit.llm.
should_failover: unreachable, 404/405, 408/429, 5xx, a rejected key, "this
API or model is not supported here"), the same call is made on the next
route; the failed route is skipped for the cooldown and then tried again.
An error that would repeat anywhere (context too long, content filtered) is
raised as it is, so CrewAI's own handling of it still works.

The agents notice nothing: tool calling, stop words and token counts are
those of the route that answered. Which routes served a run, and what failed,
is written to the run's usage ledger and printed at the end of the run.
"""

from typing import Any

from crewai import LLM, BaseLLM
from crewai.types.usage_metrics import UsageMetrics
from pydantic import PrivateAttr

from esamkit.llm import Failover, Route


def make_llm(route: Route) -> Any:
    """A CrewAI LLM for one route, over any OpenAI-compatible endpoint
    (OpenAI on the laptop, the IL5 API Gateway on the pilot)."""
    kwargs: dict[str, Any] = {"model": f"openai/{route.model}", "base_url": route.base_url, "api_key": route.api_key}
    if route.api == "responses":
        kwargs["api"] = "responses"           # CrewAI's native OpenAI provider: POST <base_url>/responses
    return LLM(**kwargs)


class FailoverLLM(BaseLLM):
    """Several CrewAI LLMs behind one: calls go to the first route that works."""

    llm_type: str = "esam_failover"

    _inner: dict = PrivateAttr(default_factory=dict)
    _failover: Any = PrivateAttr(default=None)

    @classmethod
    def over(cls, routes: list[Route], cooldown: float = 300.0, log=print, make=make_llm) -> "FailoverLLM":
        primary = routes[0]
        self = cls(model=primary.model, base_url=primary.base_url, provider="openai")
        self._inner = {r.name: make(r) for r in routes}
        self._failover = Failover(routes, cooldown=cooldown, log=log)
        return self

    # the route whose capabilities the agents are told about: the one that would serve the next call
    def _current(self) -> Any:
        return self._inner[self._failover.order()[0].name]

    def _prepared(self, route: Route) -> Any:
        """The route's LLM, carrying whatever CrewAI set on this object for the call."""
        inner = self._inner[route.name]
        inner.stop = list(self.stop_sequences)
        stream = self._effective_stream()
        if stream is not None:
            inner.stream = stream
        return inner

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
             from_agent=None, response_model=None):
        return self._failover.run(lambda route: self._prepared(route).call(
            messages, tools=tools, callbacks=callbacks, available_functions=available_functions,
            from_task=from_task, from_agent=from_agent, response_model=response_model))

    async def acall(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
                    from_agent=None, response_model=None):
        return await self._failover.arun(lambda route: self._prepared(route).acall(
            messages, tools=tools, callbacks=callbacks, available_functions=available_functions,
            from_task=from_task, from_agent=from_agent, response_model=response_model))

    def supports_function_calling(self) -> bool:
        return bool(self._current().supports_function_calling())

    def supports_stop_words(self) -> bool:
        return bool(self._current().supports_stop_words())

    def supports_multimodal(self) -> bool:
        return bool(self._current().supports_multimodal())

    def get_context_window_size(self) -> int:
        return min(inner.get_context_window_size() for inner in self._inner.values())

    def get_token_usage_summary(self) -> UsageMetrics:
        total = UsageMetrics()
        for inner in self._inner.values():
            total.add_usage_metrics(inner.get_token_usage_summary())
        return total

    def failover_summary(self) -> dict:
        return self._failover.summary()


def build_llm(routes: list[Route], cooldown: float = 300.0, log=print) -> Any:
    """One route: a plain CrewAI LLM. Several: a FailoverLLM over them."""
    if not routes:
        raise ValueError("no model endpoint is configured for the crew")
    return make_llm(routes[0]) if len(routes) == 1 else FailoverLLM.over(routes, cooldown, log)
