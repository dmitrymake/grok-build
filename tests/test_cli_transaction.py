"""Regression tests for CLI state mutations through transactions."""

from __future__ import annotations

import json
from pathlib import Path

from grokbuild import cli
from grokbuild.state import ExecutionTrack, RuntimeState, default_state_path
from grokbuild.transactions import transaction


def _seed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = RuntimeState(source_path=path)
    stale = ExecutionTrack(
        decision_id="stale",
        session_id="session",
        turn_id=1,
        updated_at=1.0,
    )
    raw = state.to_dict()
    raw["version"] = 7
    raw["history"] = [{"decision_id": "legacy-1", "role": "review"}]
    raw["executions"] = {"stale": stale.to_dict()}
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_cli_state_set_matches_transaction_migration(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("grokbuild.state.time.time", lambda: 100000.0)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "cli"))
    cli_path = default_state_path()
    _seed(cli_path)
    assert cli.main(["state", "set", "--role", "review", "--available", "false"]) == 0

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "tx"))
    tx_path = default_state_path()
    _seed(tx_path)

    def mutate(state: RuntimeState) -> None:
        state.set_available("review", False, "")

    transaction(tx_path, mutate)
    cli_raw = json.loads(cli_path.read_text(encoding="utf-8"))
    tx_raw = json.loads(tx_path.read_text(encoding="utf-8"))
    assert cli_raw == tx_raw
    assert "history" not in cli_raw
    for path in (cli_path, tx_path):
        assert (path.parent / ".decisions-migrated").is_file()
        records = (path.parent / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        assert any(json.loads(line)["decision_id"] == "legacy-1" for line in records)
        assert "stale" not in json.loads(path.read_text(encoding="utf-8"))["executions"]


def test_cli_provider_and_prune_migrate_legacy_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "provider"))
    provider_path = default_state_path()
    _seed(provider_path)
    assert cli.main(["provider", "zai", "available"]) == 0
    assert (provider_path.parent / ".decisions-migrated").is_file()

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "prune"))
    prune_path = default_state_path()
    _seed(prune_path)
    assert cli.main(["state", "prune", "--yes"]) == 0
    raw = json.loads(prune_path.read_text(encoding="utf-8"))
    assert "history" not in raw
    assert "stale" not in raw["executions"]
    assert (prune_path.parent / ".decisions-migrated").is_file()
