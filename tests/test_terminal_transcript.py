from __future__ import annotations

import json
from pathlib import Path

from grokbuild import settlement

FIXTURES = Path(__file__).parent / "fixtures"


def test_real_session_turn_ended_event_is_terminal(monkeypatch, tmp_path: Path) -> None:
    task_id = "01a00000-0000-7000-8000-000000000000"
    folder = tmp_path / task_id
    folder.mkdir()
    summary = json.loads((FIXTURES / "real-session-summary.json").read_text(encoding="utf-8"))
    (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    history = folder / "chat_history.jsonl"
    history.write_text(
        json.dumps({"type": "assistant", "content": [{"type": "text", "text": "done"}]}) + "\n",
        encoding="utf-8",
    )
    (folder / "events.jsonl").write_text(
        json.dumps({"type": "turn_ended", "ts": 0, "outcome": "completed"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settlement, "_session_dir", lambda _task_id: folder)

    assert settlement._child_transcript_is_terminal(task_id, history)


def test_later_event_after_turn_end_is_nonterminal(monkeypatch, tmp_path: Path) -> None:
    task_id = "01a00000-0000-7000-8000-000000000002"
    folder = tmp_path / task_id
    folder.mkdir()
    (folder / "summary.json").write_text(json.dumps({"session_kind": "subagent"}), encoding="utf-8")
    history = folder / "chat_history.jsonl"
    history.write_text(json.dumps({"type": "assistant", "content": "done"}) + "\n", encoding="utf-8")
    (folder / "events.jsonl").write_text(
        json.dumps({"type": "turn_ended"}) + "\n" + json.dumps({"type": "tool_started"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settlement, "_session_dir", lambda _task_id: folder)
    assert not settlement._child_transcript_is_terminal(task_id, history)


def test_malformed_events_are_nonterminal(monkeypatch, tmp_path: Path) -> None:
    task_id = "01a00000-0000-7000-8000-000000000003"
    folder = tmp_path / task_id
    folder.mkdir()
    (folder / "summary.json").write_text(json.dumps({"session_kind": "subagent"}), encoding="utf-8")
    history = folder / "chat_history.jsonl"
    history.write_text(json.dumps({"type": "assistant", "content": "done"}) + "\n", encoding="utf-8")
    (folder / "events.jsonl").write_text('{"type":"turn_ended"}\n{torn', encoding="utf-8")
    monkeypatch.setattr(settlement, "_session_dir", lambda _task_id: folder)
    assert not settlement._child_transcript_is_terminal(task_id, history)


def test_legacy_summary_status_is_terminal(monkeypatch, tmp_path: Path) -> None:
    task_id = "01a00000-0000-7000-8000-000000000004"
    folder = tmp_path / task_id
    folder.mkdir()
    (folder / "summary.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    history = folder / "chat_history.jsonl"
    history.write_text(json.dumps({"type": "assistant", "content": "done"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(settlement, "_session_dir", lambda _task_id: folder)
    assert settlement._child_transcript_is_terminal(task_id, history)


def test_assistant_final_envelope_is_terminal(monkeypatch, tmp_path: Path) -> None:
    task_id = "01a00000-0000-7000-8000-000000000005"
    folder = tmp_path / task_id
    folder.mkdir()
    (folder / "summary.json").write_text(json.dumps({"session_kind": "subagent"}), encoding="utf-8")
    history = folder / "chat_history.jsonl"
    history.write_text(
        json.dumps({"type": "assistant", "final": True, "content": "done"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settlement, "_session_dir", lambda _task_id: folder)
    assert settlement._child_transcript_is_terminal(task_id, history)


def test_unknown_session_shape_remains_nonterminal(monkeypatch, tmp_path: Path) -> None:
    task_id = "01a00000-0000-7000-8000-000000000001"
    folder = tmp_path / task_id
    folder.mkdir()
    (folder / "summary.json").write_text(json.dumps({"session_kind": "subagent"}), encoding="utf-8")
    history = folder / "chat_history.jsonl"
    history.write_text(json.dumps({"type": "assistant", "content": "done"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(settlement, "_session_dir", lambda _task_id: folder)

    assert not settlement._child_transcript_is_terminal(task_id, history)
