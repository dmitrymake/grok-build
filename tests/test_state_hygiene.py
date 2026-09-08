#!/usr/bin/env python3
"""Bounded-persistence coverage for the state file and its sidecars.

The state directory is append-heavy and long-lived. Two of its writers used to
grow or fail open without any observable signal: the remediation sidecar lost
every open obligation as soon as its size rotation fired, because the reader
only ever looked at the current generation, and the payload-debug capture
appended without a lock or a size cap. Turn bookkeeping had a matching hole:
ageing keyed on ``turn_seen`` alone could never reach a session that had turn
counters but no ``turn_seen`` entry.
"""

from __future__ import annotations

from _harness import setup_environment

from _harness import make_check

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

import json  # noqa: E402
import time  # noqa: E402

from grokbuild import persist  # noqa: E402
from grokbuild.cli import build_parser, cmd_state_prune  # noqa: E402
from grokbuild.payloads import dump_payload_debug  # noqa: E402
from grokbuild.remediation import (  # noqa: E402
    compact_remediation_debt,
    open_remediation_debt,
    open_remediation_debts,
    record_remediation_stop,
    remediation_path,
)
from grokbuild.state import (
    TURN_TTL,
    RoleStatus,
    RuntimeState,
    default_state_path,
    load_state,
    save_state,
)

from _harness import SID

FAILURES: list[str] = []

SAFE_ALTERNATIVE = {
    "kind": "required_stage",
    "description": "Run the next required stage.",
    "reversibility": "reversible",
}


check = make_check(FAILURES)


def test_atomic_state_update_redacts_sensitive_values(tmp: Path) -> None:
    """State snapshots redact credentials without changing clean payloads."""
    target = tmp / "state" / "state.json"
    payload = {"token": "supersecret", "normal": {"value": "clean"}}
    returned = persist.atomic_update_json(target, lambda _data: payload, default={})
    stored = json.loads(target.read_text(encoding="utf-8"))
    check(returned == payload, "atomic updates retain their return-value semantics")
    check(stored["token"] == "[redacted]", "sensitive state values are redacted on disk")
    check(stored["normal"] == payload["normal"], "clean state values round-trip unchanged")


def test_prune_drops_turn_bookkeeping_with_no_turn_seen(tmp: Path) -> None:
    """A session with turn counters but no ``turn_seen`` must not live forever."""
    setup_environment(tmp)
    now = time.time()
    state = RuntimeState(
        turns={"live": 4, "orphan": 9},
        prompts={"live": "key-live", "orphan": "key-orphan"},
        turn_seen={"live": now},
    )
    state.prune(now=now)
    check(state.turns == {"live": 4}, f"the orphan turn counter is dropped ({state.turns})")
    check(state.prompts == {"live": "key-live"}, "the orphan prompt fingerprint is dropped")
    check(state.turn_seen == {"live": now}, "the live session keeps its bookkeeping")


def test_prune_ages_session_circuits_on_their_own_clock(tmp: Path) -> None:
    """Circuits are opened by role failures alone, so they need their own TTL."""
    setup_environment(tmp)
    now = time.time()
    state = RuntimeState(
        session_circuits={
            "fresh": {"implement-standard": RoleStatus(False, "spawn failure", last_seen=now)},
            "stale": {
                "implement-standard": RoleStatus(
                    False, "spawn failure", last_seen=now - TURN_TTL - 1.0
                )
            },
            "empty": {},
        }
    )
    state.prune(now=now)
    check(
        set(state.session_circuits) == {"fresh"},
        f"only the recently signalled circuit survives ({sorted(state.session_circuits)})",
    )


def test_open_obligations_survive_a_sidecar_rotation(tmp: Path) -> None:
    """Rotation must not silently void the obligations the Stop gate enforces."""
    setup_environment(tmp)
    target = remediation_path()
    original = persist.MAX_LOG_BYTES
    persist.MAX_LOG_BYTES = 512
    try:
        record = open_remediation_debt(
            session_id=SID,
            decision_id="d-1",
            reason_code="wrong_stage",
            current_branch="explore",
            safe_alternative=SAFE_ALTERNATIVE,
        )
        check(record is not None, "the obligation is opened")
        for _ in range(6):
            record = record_remediation_stop(record)
        check(
            Path(f"{target}.1").is_file(),
            "the sidecar rotated at the lowered size cap",
        )
    finally:
        persist.MAX_LOG_BYTES = original
    open_debts = open_remediation_debts(SID, 86400.0)
    check(
        len(open_debts) == 1 and open_debts[0].obligation == "wrong_stage",
        f"the obligation is still visible after rotation ({open_debts})",
    )
    check(
        open_debts[0].stop_blocks == 6,
        f"the newest generation wins the fold ({open_debts[0].stop_blocks})",
    )


def test_compaction_keeps_one_record_per_obligation(tmp: Path) -> None:
    """Every append is a full snapshot, so latest-per-key is lossless."""
    setup_environment(tmp)
    target = remediation_path()
    first = open_remediation_debt(
        session_id=SID,
        decision_id="d-1",
        reason_code="wrong_stage",
        current_branch="explore",
        safe_alternative=SAFE_ALTERNATIVE,
    )
    for _ in range(4):
        first = record_remediation_stop(first)
    open_remediation_debt(
        session_id=SID,
        decision_id="d-2",
        reason_code="zero_write",
        current_branch="edit",
        safe_alternative=SAFE_ALTERNATIVE,
    )
    before, after = compact_remediation_debt(target)
    check(
        before > after and after == 2, f"compaction folds to one record per key ({before}->{after})"
    )
    lines = target.read_text(encoding="utf-8").splitlines()
    check(len(lines) == 2, f"the file holds exactly the folded records ({len(lines)})")
    debts = open_remediation_debts(SID, 86400.0)
    check(
        {debt.obligation for debt in debts} == {"wrong_stage", "zero_write"},
        f"both obligations survive compaction ({[d.obligation for d in debts]})",
    )
    check(
        next(debt for debt in debts if debt.obligation == "wrong_stage").stop_blocks == 4,
        "the folded record keeps the accumulated stop count",
    )


def test_compaction_drops_records_past_the_debt_window(tmp: Path) -> None:
    """Readers already ignore records older than the debt window."""
    setup_environment(tmp)
    target = remediation_path()
    open_remediation_debt(
        session_id=SID,
        decision_id="d-1",
        reason_code="wrong_stage",
        current_branch="explore",
        safe_alternative=SAFE_ALTERNATIVE,
        now=time.time() - 200000.0,
    )
    before, after = compact_remediation_debt(target)
    check(before == 1 and after == 0, f"the expired obligation is dropped ({before}->{after})")
    check(
        open_remediation_debts(SID, 86400.0) == (),
        "the reader agrees the obligation was already invisible",
    )


def test_payload_debug_capture_is_bounded(tmp: Path) -> None:
    """The debug capture is opt-in, but it must not grow without a limit."""
    setup_environment(tmp)
    directory = default_state_path().parent
    (directory / "payload-debug.enabled").write_text("", encoding="utf-8")
    original = persist.MAX_LOG_BYTES
    persist.MAX_LOG_BYTES = 256
    try:
        for index in range(40):
            dump_payload_debug({"toolName": f"tool-{index}"}, directory, "2026-08-31T00:00:00Z")
    finally:
        persist.MAX_LOG_BYTES = original
    capture = directory / "payload-debug.jsonl"
    check(capture.is_file(), "the capture is written when the marker is present")
    check(
        (directory / "payload-debug.jsonl.1").is_file(),
        "the capture rotates instead of growing without bound",
    )
    check(
        capture.stat().st_mode & 0o777 == 0o600,
        f"the capture keeps owner-only permissions ({oct(capture.stat().st_mode & 0o777)})",
    )
    records = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    check(all("payload" in record for record in records), "records keep their structure shape")


def test_payload_debug_stays_opt_in(tmp: Path) -> None:
    setup_environment(tmp)
    directory = default_state_path().parent
    dump_payload_debug({"toolName": "tool"}, directory, "2026-08-31T00:00:00Z")
    check(
        not (directory / "payload-debug.jsonl").exists(),
        "no capture is written without the marker file",
    )


def test_state_prune_command_reports_and_requires_confirmation(tmp: Path, capsys) -> None:
    setup_environment(tmp)
    parser = build_parser()
    args = parser.parse_args(["state", "prune", "--yes"])
    check(args.func is cmd_state_prune, "the subcommand dispatches to the prune handler")

    now = time.time()
    state = RuntimeState(source_path=default_state_path())
    state.turns = {"live": 1, "orphan": 2}
    state.turn_seen = {"live": now}
    save_state(state, default_state_path())

    open_remediation_debt(
        session_id=SID,
        decision_id="d-1",
        reason_code="wrong_stage",
        current_branch="explore",
        safe_alternative=SAFE_ALTERNATIVE,
    )
    record = open_remediation_debts(SID, 86400.0)[0]
    for _ in range(3):
        record = record_remediation_stop(record)

    capsys.readouterr()
    check(
        cmd_state_prune(parser.parse_args(["state", "prune"])) == 2, "prune refuses without --yes"
    )
    capsys.readouterr()
    check(
        cmd_state_prune(parser.parse_args(["state", "prune", "--yes", "--older-than", "0"])) == 2,
        "prune rejects a non-positive TTL",
    )
    capsys.readouterr()

    code = cmd_state_prune(args)
    captured = capsys.readouterr().out
    check(code == 0, "prune succeeds with --yes")
    report = json.loads(captured)
    check(
        report["remediation_records"]["before"] > report["remediation_records"]["after"],
        f"the report shows the sidecar shrinking ({report['remediation_records']})",
    )
    check(
        "remediation-debt-v1.jsonl" in report["after"]["sidecars"],
        "the report lists sidecar sizes",
    )
    check(
        load_state(default_state_path()).turns == {"live": 1},
        "the prune reached the orphan turn counter",
    )
    check(
        len(open_remediation_debts(SID, 86400.0)) == 1,
        "the surviving obligation is still enforceable after the prune",
    )
