from __future__ import annotations

import json
from pathlib import Path

import pytest

from grokbuild.state import RuntimeState, StateValidationError, load_state, state_from_raw


def _v8() -> dict:
    return {
        "version": 8,
        "roles": {},
        "provider_availability": {},
        "turns": {},
        "prompts": {},
        "turn_seen": {},
        "executions": {},
        "session_circuits": {},
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("executions.d.member_tasks", {"member_tasks": []}),
        ("executions.d.stop_blocks", {"stop_blocks": -1}),
        ("executions.d.updated_at", {"updated_at": "now"}),
        ("roles.r.requested", {"roles": {"r": {"requested": -1}}}),
    ],
)
def test_malformed_v8_shapes_raise_typed_error(path: str, value: dict) -> None:
    data = _v8()
    if path.startswith("executions.d."):
        data["executions"] = {"d": {"stages": [], **value}}
    else:
        data.update(value)
    with pytest.raises(StateValidationError, match=path):
        RuntimeState.from_dict(data)


def test_invalid_state_is_quarantined_and_load_continues(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    data = _v8()
    data["executions"] = {"d": {"member_tasks": []}}
    target.write_text(json.dumps(data), encoding="utf-8")
    state = load_state(target)
    assert state.executions == {}
    assert list(tmp_path.glob("state.json.corrupt-*"))
    event = json.loads((tmp_path / "state.json.quarantine.jsonl").read_text().splitlines()[0])
    assert "executions.d.member_tasks" in event["reason"]


def test_non_string_task_keys_raise_typed_error() -> None:
    data = _v8()
    data["executions"] = {"d": {"member_tasks": {1: "task"}}}
    with pytest.raises(StateValidationError, match=r"executions.d.member_tasks"):
        RuntimeState.from_dict(data)


def test_unknown_stage_kind_is_forward_compatible(tmp_path: Path) -> None:
    data = _v8()
    data["executions"] = {
        "d": {"stages": [{"role": "future", "required": True, "kind": "future_kind"}]}
    }
    target = tmp_path / "state.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    state = load_state(target)
    assert not list(tmp_path.glob("state.json.corrupt-*"))
    track = state.executions["d"]
    assert track.stages[0].kind == "future_kind"
    assert track.stages[0].spawnable


def test_atomic_update_shape_failure_quarantines_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text(json.dumps({**_v8(), "executions": []}), encoding="utf-8")
    recovered = state_from_raw(json.loads(target.read_text()), target)
    assert recovered.executions == {}
    assert list(tmp_path.glob("state.json.corrupt-*"))
