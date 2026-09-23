"""Agent tests: prompt assembly and tolerant JSON extraction.

The JSON tests matter more than they look: free models are unreliable about
structured output, so every later phase depends on being able to salvage a JSON
object out of a messy reply.
"""

from __future__ import annotations

import pytest

from backend.core.agents import Agent, extract_json_block, fenced_blocks
from backend.tests.helpers import build_registry, build_router, mock_provider


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('```json\n{"task": "build", "n": 2}\n```', {"task": "build", "n": 2}),
        ('```\n{"ok": true}\n```', {"ok": True}),
        ('Here is the plan:\n{"steps": ["a", "b"]}\nHope that helps!', {"steps": ["a", "b"]}),
        ('{"already": "pure json"}', {"already": "pure json"}),
        ('text before ```json\n{"a": {"b": 1}}\n``` text after', {"a": {"b": 1}}),
        ('{"note": "a } brace inside a string"}', {"note": "a } brace inside a string"}),
    ],
)
def test_extract_json_block_finds_json(text: str, expected: dict) -> None:
    assert extract_json_block(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "no json here at all", "```json\n{broken: true}\n```", "{\"unclosed\": "],
)
def test_extract_json_block_returns_none_when_hopeless(text: str) -> None:
    assert extract_json_block(text) is None


def test_fenced_blocks_returns_every_block() -> None:
    text = "intro\n```python\nprint(1)\n```\nmiddle\n```json\n{}\n```"
    blocks = fenced_blocks(text)
    assert blocks == ["print(1)", "{}"]


def test_build_messages_puts_context_between_system_and_user(tmp_path) -> None:
    registry = build_registry([mock_provider("p")])
    router, bus = build_router(tmp_path, registry)
    agent = Agent(registry.agent("tester_agent"), router=router, bus=bus, root=registry.root)

    messages = agent.build_messages("do the thing", context={"task_board": ["a", "b"]})

    assert [m.role for m in messages] == ["system", "system", "user"]
    assert "CONTEXT" in messages[1].content
    assert "task_board" in messages[1].content
    assert messages[2].content == "do the thing"


def test_agent_run_emits_start_and_end_events(tmp_path) -> None:
    registry = build_registry([mock_provider("p", reply='```json\n{"ok": true}\n```')])
    router, bus = build_router(tmp_path, registry)
    agent = Agent(registry.agent("tester_agent"), router=router, bus=bus, root=registry.root)

    result = agent.run("give me json")

    assert result.parsed == {"ok": True}
    kinds = [record["kind"] for record in bus.jsonl.read_all()]
    assert kinds == ["agent.start", "provider.attempt", "llm.response", "agent.end"]
    assert result.completion.provider == "p"
