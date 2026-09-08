"""Golden serialization coverage for the version-eight runtime state."""

from __future__ import annotations

from _harness import run_standalone

import json
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

from grokbuild.decision import ExecutionMember, ExecutionStage  # noqa: E402
from grokbuild.state import (
    ExecutionTrack,
    ProviderAvailability,
    RoleStatus,
    RuntimeState,
    save_state,
)

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "state-v8-golden.json"


def build_state() -> RuntimeState:
    stages = (
        ExecutionStage("explore", True, "recon", kind="spawn", stage_id="recon", slot="recon"),
        ExecutionStage(
            "implement-standard",
            True,
            "implementation",
            kind="spawn",
            stage_id="impl",
            slot="impl",
        ),
        ExecutionStage(
            "recon",
            True,
            "parallel recon",
            kind="parallel_spawn",
            stage_id="recon-fanout",
            members=(
                ExecutionMember("a", "explore", True, "first"),
                ExecutionMember("b", "explore-thorough", True, "second"),
            ),
            slot="recon",
        ),
        ExecutionStage(
            "review",
            True,
            "review barrier",
            kind="parallel_spawn",
            stage_id="review-panel",
            members=(
                ExecutionMember("one", "review", True, "panel one"),
                ExecutionMember("two", "review-independent", True, "panel two"),
            ),
            slot="review",
        ),
        *tuple(
            ExecutionStage(
                "verify",
                True,
                f"verify step {index}",
                kind="verify",
                stage_id=f"verify/{index}",
                command=f"./verify-{index}.sh",
                slot="verify",
            )
            for index in range(3)
        ),
    )
    track = ExecutionTrack(
        "decision-saturated",
        "session-main",
        7,
        stages=stages,
        requested=["recon-fanout/a", "recon-fanout/b", "review-panel/one", "explore"],
        completed=["recon-fanout/b", "review-panel/one", "explore"],
        failed=["impl"],
        verified=["verify/0"],
        member_tasks={"recon-fanout/a": "job-recon-a"},
        stage_tasks={"explore": "job-explore"},
        terminal_tasks={"job-terminal": "impl"},
        verify_tasks={"job-verify": "verify/1"},
        requested_at={"recon-fanout/a": 1700000001.25, "explore": 1700000002.5},
        repair_failures={"impl": 2, "verify/1": 1},
        not_found_streaks={"impl": {"task_id": "job-terminal", "count": 1}},
        failure_reasons={"impl": "compile failure"},
        warnings=["retry pending"],
        migrated_dropped=["completed:old"],
        stop_blocks=3,
        updated_at=2000000003.75,
    )
    legacy = ExecutionTrack(
        "decision-legacy",
        "session-old",
        2,
        stages=(ExecutionStage("plan", True, "legacy stage"),),
        requested=["plan"],
        updated_at=2000000004.0,
    )
    return RuntimeState(
        version=8,
        roles={
            "explore": RoleStatus(
                True, "", "operator", 1, 4, 2, 8, 5, 4, 12, 20, 0.0, 2000000001.0
            ),
            "implement-standard": RoleStatus(
                False, "quota", "", 3, 0, 7, 1, 8, 1, 20, 20, 2000000300.0, 2000000002.0
            ),
        },
        provider_availability={
            "sample": ProviderAvailability(True, 0.0, "", 2000000000.5),
            "grok": ProviderAvailability(False, 2000000300.0, "maintenance", 2000000000.75),
        },
        turns={"session-main": 7, "session-old": 2},
        prompts={"session-main": "prompt-key-main", "session-old": "prompt-key-old"},
        turn_seen={"session-main": 2000000001.5, "session-old": 2000000001.75},
        executions={track.decision_id: track, legacy.decision_id: legacy},
        session_circuits={
            "session-main": {
                "implement-standard": RoleStatus(
                    False,
                    "spawn failure",
                    "",
                    3,
                    0,
                    3,
                    0,
                    0,
                    0,
                    0,
                    None,
                    2000000300.0,
                    2000000002.25,
                )
            }
        },
        updated_at=2000000005.0,
    )


def test_state_v8_golden() -> None:
    state = build_state()
    expected = FIXTURE.read_text(encoding="utf-8")
    assert json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n" == expected
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.json"
        save_state(state, path)
        assert path.read_bytes() == FIXTURE.read_bytes()


def main() -> int:
    test_state_v8_golden()
    return 0


if __name__ == "__main__":
    run_standalone(main)
