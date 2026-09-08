from __future__ import annotations

from grokbuild.payloads import terminal_background_ack


def test_foreground_verifier_text_with_task_marker_is_not_ack() -> None:
    assert not terminal_background_ack({"toolResult": "done <task-id>verifier-output</task-id>"})


def test_structured_background_started_with_task_id_is_ack() -> None:
    assert terminal_background_ack(
        {
            "toolResult": {
                "type": "BackgroundTaskStarted",
                "task_ids": ["background-1"],
            }
        }
    )
