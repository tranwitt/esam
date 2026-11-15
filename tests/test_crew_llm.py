"""The crew's model adapter (crew/llm.py): one route is a plain CrewAI LLM,
several are a FailoverLLM. Needs CrewAI; the failover rules themselves are
tested without it in tests/test_llm.py."""

import pytest

pytest.importorskip("crewai")

from crew.llm import FailoverLLM, build_llm, make_llm          # noqa: E402
from esamkit.llm import EndpointError, Route                    # noqa: E402

A = Route("http://a.invalid/v1", "gpt-5.1", "ka", "chat", "primary")
B = Route("http://a.invalid/v1", "gpt-5.1", "ka", "responses", "fallback 1")


class Inner:
    def __init__(self, route, fail=None):
        self.route, self.fail, self.calls, self.stop, self.stream = route, fail, 0, [], False

    def call(self, messages, **kw):
        self.calls += 1
        if self.fail:
            raise self.fail
        return f"answer from {self.route.name}"

    def supports_function_calling(self):
        return self.route.api == "chat"

    def supports_stop_words(self):
        return True

    def supports_multimodal(self):
        return False

    def get_context_window_size(self):
        return 8000 if self.route.api == "chat" else 4000

    def get_token_usage_summary(self):
        from crewai.types.usage_metrics import UsageMetrics
        return UsageMetrics(total_tokens=10 * self.calls, successful_requests=self.calls)


def test_one_route_is_a_plain_llm_and_responses_selects_that_api():
    llm = build_llm([A])
    assert not isinstance(llm, FailoverLLM) and llm.model.endswith("gpt-5.1")
    assert getattr(make_llm(B), "api", "responses") == "responses"
    with pytest.raises(ValueError):
        build_llm([])


def test_several_routes_fail_over_and_report():
    made, lines = {}, []

    def make(route):
        made[route.name] = Inner(route, EndpointError(route.base_url, 503, "down") if route is A else None)
        return made[route.name]

    llm = FailoverLLM.over([A, B], cooldown=60, log=lines.append, make=make)
    assert llm.supports_function_calling() is True                     # the primary's, while it is up
    assert llm.call([{"role": "user", "content": "hi"}]) == "answer from fallback 1"
    assert llm.call([{"role": "user", "content": "again"}]) == "answer from fallback 1"
    assert made["primary"].calls == 1 and made["fallback 1"].calls == 2    # primary skipped while it cools down
    assert llm.supports_function_calling() is False and llm.get_context_window_size() == 4000
    assert llm.get_token_usage_summary().total_tokens == 30 and lines
    assert llm.failover_summary()


def test_an_error_that_would_repeat_anywhere_is_raised_as_it_is():
    boom = ValueError("context length exceeded")
    made = {}

    def make(route):
        made[route.name] = Inner(route, boom if route is A else None)
        return made[route.name]

    llm = FailoverLLM.over([A, B], log=lambda *_: None, make=make)
    with pytest.raises(ValueError):
        llm.call([{"role": "user", "content": "hi"}])
    assert made["fallback 1"].calls == 0
