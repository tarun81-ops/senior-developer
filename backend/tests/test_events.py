"""Event log and event bus tests.

The event log is the contract between the backend and the Phase 4 desktop UI, so
it gets its own tests: record shape, crash tolerance and live subscribers.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.core.events import EventBus, JsonlWriter, format_event, iter_records


def test_writer_appends_and_reads_back(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path / "runs" / "x" / "events.jsonl")
    writer.write({"kind": "a", "message": "first"})
    writer.write({"kind": "b", "message": "second"})

    records = writer.read_all()
    assert [r["kind"] for r in records] == ["a", "b"]
    assert writer.tail(1)[0]["message"] == "second"


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"kind": "good"}\nnot json at all\n{"kind": "also good"}\n', encoding="utf-8")

    kinds = [record["kind"] for record in iter_records(path)]
    assert kinds == ["good", "also good"]


def test_event_bus_writes_jsonl_and_notifies_subscribers(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    bus = EventBus(run_id="run-1", jsonl=JsonlWriter(path), echo=False)
    seen: list[str] = []
    unsubscribe = bus.subscribe(lambda event: seen.append(event.kind))

    bus.emit("provider.attempt", "attempt 1/3", agent="code", provider="gemini", model="m")
    unsubscribe()
    bus.emit("llm.response", "done", agent="code")

    assert seen == ["provider.attempt"]
    records = JsonlWriter(path).read_all()
    assert [r["kind"] for r in records] == ["provider.attempt", "llm.response"]
    assert records[0]["run_id"] == "run-1"
    assert records[0]["provider"] == "gemini"
    assert records[0]["data"] == {}


def test_a_broken_subscriber_cannot_kill_a_run(tmp_path: Path) -> None:
    bus = EventBus(run_id="r", jsonl=JsonlWriter(tmp_path / "e.jsonl"), echo=False)
    survived: list[str] = []

    def explode(event) -> None:
        raise RuntimeError("listener is broken")

    bus.subscribe(explode)
    bus.subscribe(lambda event: survived.append(event.kind))

    bus.emit("llm.response", "still works")

    assert survived == ["llm.response"]


def test_child_bus_keeps_sinks_but_changes_the_run_id(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    bus = EventBus(run_id="parent", jsonl=JsonlWriter(path), echo=False)
    received: list[str] = []
    bus.subscribe(lambda event: received.append(event.run_id))

    child = bus.child(run_id="child")
    child.emit("run.start", "hello")

    assert received == ["child"]
    assert JsonlWriter(path).read_all()[0]["run_id"] == "child"


def test_format_event_is_one_readable_line(tmp_path: Path) -> None:
    bus = EventBus(run_id="r", jsonl=None, echo=False)
    event = bus.emit("provider.retry", "waiting 1.5s", agent="code", provider="groq", model="m")

    line = format_event(event)
    assert "provider.retry" in line
    assert "groq/m" in line
    assert "[code]" in line
    assert "\n" not in line


def test_events_are_json_serialisable(tmp_path: Path) -> None:
    bus = EventBus(run_id="r", jsonl=JsonlWriter(tmp_path / "e.jsonl"), echo=False)
    event = bus.emit("llm.response", "ok", tokens=123, nested={"a": [1, 2, 3]})

    payload = json.dumps(event.to_dict())
    assert '"tokens": 123' in payload
