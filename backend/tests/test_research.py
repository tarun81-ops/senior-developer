"""Web research for every agent (D42), against a fake Firecrawl. No network."""

from __future__ import annotations

import json

import httpx

from backend.core import research
from backend.core.agents import Agent
from backend.core.research import Researcher
from backend.tests.helpers import build_registry, build_router, mock_provider

KEY = "fc-test-" + "k" * 24
ASK = '{"web_search": ["fastapi lifespan", "  "]}'


def fake_firecrawl(status: int = 200):
    calls: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                      "body": json.loads(request.content)})
        if status != 200:
            return httpx.Response(status, json={"success": False, "error": f"echo {KEY}"})
        web = [{"title": "Lifespan", "url": "https://example.test/l", "markdown": "x" * 10_000}]
        return httpx.Response(200, json={"success": True, "data": {"web": web}})

    return calls, httpx.MockTransport(handle)


def agent_with(tmp_path, reply: str, researcher: Researcher | None):
    registry = build_registry([mock_provider("p", reply=reply)])
    router, bus = build_router(tmp_path, registry)
    sent: list[list] = []
    complete = router.complete

    def recording(agent, messages, **kwargs):
        sent.append(messages)
        return complete(agent, messages, **kwargs)

    router.complete = recording
    agent = Agent(registry.agent("tester_agent"), router=router, bus=bus,
                  root=registry.root, researcher=researcher)
    return agent, bus, sent


def test_an_agent_that_asks_gets_results_and_answers_again(tmp_path) -> None:
    calls, transport = fake_firecrawl()
    agent, bus, sent = agent_with(tmp_path, ASK, Researcher(KEY, transport=transport))

    agent.run("build it")

    # one search (the blank query dropped), key only in the header
    assert len(calls) == 1
    assert calls[0]["url"] == research.SEARCH_URL
    assert calls[0]["auth"] == f"Bearer {KEY}"
    assert calls[0]["body"]["query"] == "fastapi lifespan"
    # first call offers research; the second carries results, capped, as untrusted, with no offer
    assert len(sent) == 2
    assert "WEB RESEARCH" in sent[0][0].content
    assert "WEB RESEARCH" not in sent[1][0].content
    context = sent[1][1].content
    assert "untrusted reference material" in context
    assert "Source: https://example.test/l" in context
    assert "x" * research.PAGE_CHARS in context and "x" * (research.PAGE_CHARS + 1) not in context
    events = [r for r in bus.jsonl.read_all() if r["kind"] == "agent.research"]
    assert events[0]["data"]["searches"] == [{"query": "fastapi lifespan", "results": 1}]
    assert KEY not in json.dumps(bus.jsonl.read_all(), default=str)


def test_without_a_key_agents_are_never_offered_research(tmp_path) -> None:
    agent, _, sent = agent_with(tmp_path, ASK, None)
    result = agent.run("build it")
    assert len(sent) == 1 and "WEB RESEARCH" not in sent[0][0].content
    assert result.parsed == {"web_search": ["fastapi lifespan", "  "]}
    assert Researcher.from_env() is None  # conftest clears FIRECRAWL_API_KEY


def test_a_normal_answer_costs_no_search(tmp_path) -> None:
    calls, transport = fake_firecrawl()
    agent, _, sent = agent_with(tmp_path, '{"ok": true}', Researcher(KEY, transport=transport))
    assert agent.run("x").parsed == {"ok": True}
    assert calls == [] and len(sent) == 1


def test_a_failed_search_reports_the_status_never_the_body(tmp_path) -> None:
    _, transport = fake_firecrawl(status=401)
    agent, bus, sent = agent_with(tmp_path, ASK, Researcher(KEY, transport=transport))
    agent.run("x")
    assert "(search failed: HTTP 401)" in sent[1][1].content
    assert KEY not in sent[1][1].content
    assert KEY not in json.dumps(bus.jsonl.read_all(), default=str)


def test_the_run_wide_limit_stops_searching_and_stops_offering(tmp_path) -> None:
    calls, transport = fake_firecrawl()
    researcher = Researcher(KEY, transport=transport)
    _, summary = researcher.search([f"q{i}" for i in range(10)])
    assert len(summary) == research.MAX_QUERIES  # per request
    researcher.searches = research.MAX_SEARCHES - 1
    _, summary = researcher.search(["a", "b"])
    assert [s.get("error") for s in summary] == [None, "limit"]
    assert len(calls) == research.MAX_QUERIES + 1
    agent, _, sent = agent_with(tmp_path, ASK, researcher)
    agent.run("x")
    assert len(sent) == 1 and "WEB RESEARCH" not in sent[0][0].content
