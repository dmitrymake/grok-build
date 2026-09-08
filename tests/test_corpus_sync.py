#!/usr/bin/env python3
"""Deterministic contract tests for corpus_sync.py."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

import json
import os
import subprocess
import tempfile
from urllib.parse import quote

import grokbuild.corpus_sync as corpus_sync
import grokbuild.gate as gate


def ok(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}")
        raise AssertionError(message)
    print(f"OK: {message}")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def invoke(root: Path, command: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "grokbuild.corpus_sync",
            command,
            "--route-log",
            str(root / "route.jsonl"),
            "--sessions-root",
            str(root / "sessions"),
            "--corpus",
            str(root / "corpus.json"),
            "--staging",
            str(root / "staging.json"),
            "--claude-root",
            str(root / "claude"),
            "--watermark",
            str(root / "watermark.json"),
            "--no-git-check",
            *extra,
        ],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "GROK_HOME": str(root), "PYTHONPATH": str(REPO_ROOT)},
    )


def test_classify_provenance() -> int:
    classify = corpus_sync.classify_provenance
    ok(
        classify("/review", origin_kind="bot", prompt_source="typed")
        == ("automation", "automation-metadata"),
        "classifier metadata beats syntax and provenance",
    )
    ok(
        classify(
            "You are the implement-standard station...", origin_kind="human", prompt_source="typed"
        )
        == ("automation", "automation-syntax"),
        "classifier child brief syntax beats provenance",
    )
    structured = "## Task\nDo NOT edit files. Report back after you run the tests."
    ok(
        classify(structured, origin_kind="human", prompt_source="typed")
        == ("automation", "automation-brief-structure"),
        "classifier structured brief beats provenance",
    )
    ok(
        classify("typed request", origin_kind="human", prompt_source="typed")
        == ("human-typed", "human-provenance"),
        "classifier accepts explicit human provenance",
    )
    ok(
        classify("почини красный тест пожалуйста") == ("human-typed", "human-weak-textual"),
        "classifier accepts short Cyrillic-dominant prompt without provenance",
    )
    ok(
        classify("почини тест", origin_kind="human") == ("uncertain", "uncertain-fallback"),
        "classifier weak textual rule requires missing provenance fields",
    )
    ok(
        classify("please investigate the failed test") == ("uncertain", "uncertain-fallback"),
        "classifier has uncertain fallback",
    )
    for prompt in (
        "prefix [[GROK_ROUTE_STOP_FEEDBACK:v1]]",
        "/corpus-sync",
        "<system-reminder>x</system-reminder>",
    ):
        ok(
            classify(prompt) == ("automation", "automation-syntax"),
            "classifier rejects automation syntax",
        )


def test_corpus_sync() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sessions = root / "sessions" / "encoded workspace" / "sid-1"
        sessions.mkdir(parents=True)
        records = [
            {"type": "user", "synthetic_reason": "feedback", "content": "skip"},
            {
                "type": "user",
                "content": [
                    {
                        "type": "text",
                        "text": gate.station_recipe({"intent": "implement"}),
                    }
                ],
            },
            {"type": "user", "content": "<system-reminder>harness-only context</system-reminder>"},
            {
                "type": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<user_query>Firmware router device fix immediately please sk-abcdef1234567890 now</user_query>",
                    }
                ],
            },
            {"type": "user", "content": "second turn"},
        ]
        write_json(
            sessions / "summary.json", {"session_kind": "interactive", "info": {"cwd": str(root)}}
        )
        (sessions / "chat_history.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in records) + "\n", encoding="utf-8"
        )
        route = {
            "event": "user_prompt_submit",
            "session_id": "sid-1",
            "turn_id": 1,
            "intent": None,
            "complexity": "low",
            "risk": "low",
            "role": None,
            "execution": {"stages": [{"role": "explore", "kind": "spawn", "stage_id": "recon"}]},
            "warnings": ["x"],
            "observed_at": "2026-01-01T00:00:00Z",
        }
        (root / "route.jsonl").write_text(json.dumps(route) + "\n", encoding="utf-8")
        write_json(
            root / "corpus.json",
            {
                "version": 1,
                "cases": [
                    {
                        "id": "fw-01",
                        "prompt": "Firmware router device fix now",
                        "expect": {"intent": "implement", "role": "implement-standard"},
                    }
                ],
            },
        )
        result = invoke(root, "harvest")
        ok(result.returncode == 0, "harvest succeeds")
        staging = json.loads((root / "staging.json").read_text())
        ok(
            len(staging) == 1 and staging[0]["turn_id"] == 1,
            "Nth real turn recovery skips synthetic and stop-feedback records",
        )
        item = staging[0]
        ok(
            item["id"].startswith("fw-")
            and item["observed"]["warnings_count"] == 1
            and item["observed"]["stages"][0]["stage_id"] == "recon",
            "observed fields and category id are recorded",
        )
        ok(
            item["expect"] is None and item["flag"] == "negative-candidate",
            "null observed intent takes negative-candidate precedence",
        )
        ok(
            "sk-[redacted]" not in item["prompt"] and "[redacted]" in item["prompt"],
            "prompt secrets are redacted",
        )
        ok(
            "<user_query>" not in item["prompt"] and item["prompt"].startswith("Firmware"),
            "wrapped user query is unwrapped",
        )
        again = invoke(root, "harvest")
        ok(
            again.returncode == 0 and len(json.loads((root / "staging.json").read_text())) == 1,
            "harvest is idempotent",
        )

        sub = root / "sessions" / "sub" / "sid-sub"
        sub.mkdir(parents=True)
        write_json(sub / "summary.json", {"session_kind": "subagent"})
        (sub / "chat_history.jsonl").write_text(
            json.dumps({"type": "user", "content": "must skip"}) + "\n"
        )
        route["session_id"] = "sid-sub"
        route["turn_id"] = 1
        with (root / "route.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(route) + "\n")
        ok(
            invoke(root, "harvest").returncode == 0
            and len(json.loads((root / "staging.json").read_text())) == 1,
            "subagent sessions are skipped",
        )

        # Rotation consumes the unread tail before starting the replacement.
        rotated_session = root / "sessions" / "rotate" / "sid-rotate"
        rotated_session.mkdir(parents=True)
        write_json(
            rotated_session / "summary.json",
            {"session_kind": "interactive", "info": {"cwd": str(root)}},
        )
        rotated_prompts = [
            "alpha telemetry unique apples",
            "beta telemetry distinct bananas",
            "gamma telemetry separate cherries",
        ]
        (rotated_session / "chat_history.jsonl").write_text(
            "\n".join(json.dumps({"type": "user", "content": prompt}) for prompt in rotated_prompts)
            + "\n",
            encoding="utf-8",
        )
        rotate_route = {
            **route,
            "session_id": "sid-rotate",
            "turn_id": 1,
            "intent": "implement",
            "role": "implement-standard",
        }
        with (root / "route.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(rotate_route) + "\n")
        ok(invoke(root, "harvest").returncode == 0, "rotation baseline is harvested")
        rotate_route["turn_id"] = 2
        with (root / "route.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(rotate_route) + "\n")
        (root / "route.jsonl").rename(root / "route.jsonl.1")
        rotate_route["turn_id"] = 3
        (root / "route.jsonl").write_text(json.dumps(rotate_route) + "\n", encoding="utf-8")
        ok(
            invoke(root, "harvest").returncode == 0,
            "rotated unread tail and replacement are harvested",
        )
        rotated_items = [
            item
            for item in json.loads((root / "staging.json").read_text())
            if item["session_id"] == "sid-rotate"
        ]
        ok(
            [item["turn_id"] for item in rotated_items] == [1, 2, 3],
            "rotation harvests old and new records exactly once",
        )

        # A reused inode with a changed head is a replacement, not a continuation.
        with tempfile.TemporaryDirectory() as inode_td:
            inode_root = Path(inode_td)
            for sid, prompt in (
                ("sid-old", "old record"),
                ("sid-new", "new record one"),
                ("sid-new", "new record two"),
            ):
                session = inode_root / "sessions" / "workspace" / sid
                session.mkdir(parents=True, exist_ok=True)
                write_json(session / "summary.json", {"session_kind": "interactive"})
                content = [prompt] if sid == "sid-old" else ["new record one", "new record two"]
                (session / "chat_history.jsonl").write_text(
                    "".join(
                        json.dumps({"type": "user", "content": item}) + "\n" for item in content
                    ),
                    encoding="utf-8",
                )
            old_record = {
                **route,
                "session_id": "sid-old",
                "turn_id": 1,
                "intent": "implement",
                "role": "implement-standard",
            }
            new_records = [
                {
                    **route,
                    "session_id": "sid-new",
                    "turn_id": turn,
                    "intent": "implement",
                    "role": "implement-standard",
                }
                for turn in (1, 2)
            ]
            inode_route = inode_root / "route.jsonl"
            inode_route.write_text(json.dumps(old_record) + "\n", encoding="utf-8")
            ok(invoke(inode_root, "harvest").returncode == 0, "same-inode baseline is harvested")
            inode_route.write_text(
                "".join(json.dumps(record) + "\n" for record in new_records), encoding="utf-8"
            )
            ok(invoke(inode_root, "harvest").returncode == 0, "same-inode replacement is harvested")
            inode_items = json.loads((inode_root / "staging.json").read_text())
            ok(
                [(item["session_id"], item["turn_id"]) for item in inode_items]
                == [("sid-old", 1), ("sid-new", 1), ("sid-new", 2)],
                "changed head does not skip early replacement records",
            )

        # Manual merge preserves the corpus contract and removes only moved entries.
        staging[0]["expect"] = {"intent": "implement", "role": "implement-standard"}
        staging[0]["flag"] = None
        write_json(root / "staging.json", staging)
        before = (root / "corpus.json").read_text()
        merged = invoke(root, "merge")
        corpus = json.loads((root / "corpus.json").read_text())
        ok(
            merged.returncode == 0 and not json.loads((root / "staging.json").read_text()),
            "eligible entry merges and leaves staging",
        )
        ok(
            corpus["version"] == 1
            and all(set(x) <= {"id", "prompt", "expect", "flag"} for x in corpus["cases"]),
            "merged corpus preserves exact case shape",
        )
        ok(
            '\n  "version": 1' in (root / "corpus.json").read_text() and before,
            "corpus is formatted with indentation and trailing newline",
        )

        bad = {"id": "ms-99", "prompt": "bad", "expect": {"intent": "not-valid", "role": None}}
        write_json(root / "staging.json", [bad])
        ok(invoke(root, "merge").returncode == 1, "invalid intent is refused")
        bad["expect"] = {"intent": "implement", "role": "implement-standard"}
        bad["id"] = corpus["cases"][-1]["id"]
        write_json(root / "staging.json", [bad])
        ok(invoke(root, "merge").returncode == 1, "duplicate id is refused")

        with tempfile.TemporaryDirectory() as pending_td:
            pending_root = Path(pending_td)
            pending_records = [
                {
                    **route,
                    "session_id": f"sid-pending-{index}",
                    "turn_id": 1,
                    "intent": "implement",
                    "role": "implement-standard",
                }
                for index in range(205)
            ]
            (pending_root / "route.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in pending_records), encoding="utf-8"
            )
            first_key = [pending_records[0]["session_id"], pending_records[0]["turn_id"]]
            first = invoke(pending_root, "harvest")
            pending_state = json.loads((pending_root / "watermark.json").read_text())
            ok(
                first.returncode == 0
                and len(pending_state["pending"]) == 200
                and pending_state["pending"][0]["session_id"] == first_key[0],
                "205 unresolved records are capped without dropping the first",
            )
            ok(
                pending_state["pending_dropped"] == 5
                and pending_state["offset"] == 0
                and first_key not in pending_state["keys"],
                "pending drop accounting and watermark preserve the first unresolved record",
            )
            pending_session = pending_root / "sessions" / "workspace" / first_key[0]
            pending_session.mkdir(parents=True)
            write_json(pending_session / "summary.json", {"session_kind": "interactive"})
            (pending_session / "chat_history.jsonl").write_text(
                json.dumps({"type": "user", "content": "pending prompt recovered later"}) + "\n",
                encoding="utf-8",
            )
            second = invoke(pending_root, "harvest")
            pending_items = json.loads((pending_root / "staging.json").read_text())
            pending_state = json.loads((pending_root / "watermark.json").read_text())
            ok(
                second.returncode == 0
                and len(pending_items) == 1
                and first_key in pending_state["keys"],
                "first pending record resolves and is committed",
            )
            second_offset = len(json.dumps(pending_records[0]) + "\n")
            ok(
                pending_state["keys"].count(first_key) == 1
                and pending_state["offset"] == second_offset,
                "resolved pending key is committed exactly once while later records remain unresolved",
            )
            ok(
                invoke(pending_root, "harvest").returncode == 0
                and len(json.loads((pending_root / "staging.json").read_text())) == 1,
                "resolved pending record is not duplicated",
            )

        with tempfile.TemporaryDirectory() as duplicate_td:
            duplicate_root = Path(duplicate_td)
            duplicate_session = duplicate_root / "sessions" / "workspace" / "sid-near"
            duplicate_session.mkdir(parents=True)
            write_json(duplicate_session / "summary.json", {"session_kind": "interactive"})
            (duplicate_session / "chat_history.jsonl").write_text(
                json.dumps({"type": "user", "content": "deploy service quickly please now"}) + "\n"
            )
            duplicate_route = {
                **route,
                "session_id": "sid-near",
                "turn_id": 1,
                "intent": "implement",
                "role": "implement-standard",
            }
            (duplicate_root / "route.jsonl").write_text(json.dumps(duplicate_route) + "\n")
            write_json(duplicate_root / "corpus.json", {"version": 1, "cases": []})
            write_json(
                duplicate_root / "staging.json",
                [{"id": "ms-1", "prompt": "deploy service quickly now", "expect": None}],
            )
            near = invoke(duplicate_root, "harvest")
            near_items = json.loads((duplicate_root / "staging.json").read_text())
            ok(
                near.returncode == 0 and near_items[-1]["flag"] == "near-dup:ms-1",
                "null-expect staging near-duplicate is flagged without drift crash",
            )

        with tempfile.TemporaryDirectory() as redact_td:
            redact_root = Path(redact_td)
            redact_session = redact_root / "sessions" / "workspace" / "sid-redact"
            redact_session.mkdir(parents=True)
            write_json(redact_session / "summary.json", {"session_kind": "interactive"})
            secret = "sk-abcdefghijklmnopqrst"
            boundary_prompt = "x " * 195 + secret + " trailing text"
            (redact_session / "chat_history.jsonl").write_text(
                json.dumps({"type": "user", "content": boundary_prompt}) + "\n"
            )
            redact_route = {
                **route,
                "session_id": "sid-redact",
                "turn_id": 1,
                "intent": "implement",
                "role": "implement-standard",
            }
            (redact_root / "route.jsonl").write_text(json.dumps(redact_route) + "\n")
            redacted = invoke(redact_root, "harvest")
            redacted_prompt = json.loads((redact_root / "staging.json").read_text())[0]["prompt"]
            ok(
                redacted.returncode == 0
                and secret not in redacted_prompt
                and "[redacted]" in redacted_prompt,
                "secret crossing truncation boundary is fully redacted",
            )
            ok(len(redacted_prompt) <= 400, "redacted prompt is truncated after scrubbing")

        with tempfile.TemporaryDirectory() as history_td:
            history_root = Path(history_td)
            grok_root = history_root / "sessions"
            (grok_root / "plain workspace").mkdir(parents=True)
            (grok_root / "рабочее%20место").mkdir(parents=True)
            bash_session = grok_root / "plain workspace" / "sid-bash"
            subagent_session = grok_root / "plain workspace" / "sid-child"
            bash_session.mkdir()
            subagent_session.mkdir()
            write_json(bash_session / "summary.json", {"session_kind": "interactive"})
            write_json(subagent_session / "summary.json", {"session_kind": "subagent"})
            (grok_root / "plain workspace" / "prompt_history.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "prompt": "почини красный тест пожалуйста",
                    }
                )
                + "\n"
                + json.dumps({"timestamp": "2026-01-01T00:00:01Z", "prompt": "ок"})
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "prompt": "почини bash командой пожалуйста",
                        "is_bash": True,
                        "session_id": "sid-bash",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:03Z",
                        "prompt": "почини дочернюю задачу пожалуйста",
                        "session_id": "sid-child",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (grok_root / "рабочее%20место" / "prompt_history.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "prompt": "это уникальная задача для unicode workspace",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            claude = history_root / "claude" / "-tmp-project"
            claude.mkdir(parents=True)
            records = [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "claude human prompt"},
                    "cwd": "/tmp/project",
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "tool result"}],
                    },
                },
                {
                    "type": "user",
                    "isMeta": True,
                    "message": {"role": "user", "content": "metadata generated prompt"},
                },
                {
                    "type": "user",
                    "isSidechain": True,
                    "message": {"role": "user", "content": "sidechain generated prompt"},
                },
                {
                    "type": "user",
                    "message": {"role": "user", "content": "<command-name>ls</command-name>"},
                },
                {
                    "type": "user",
                    "origin": {"kind": "human"},
                    "promptSource": "typed",
                    "cwd": "/tmp/project",
                    "message": {"role": "user", "content": "typed origin prompt"},
                },
                {
                    "type": "user",
                    "origin": {"kind": "human"},
                    "promptSource": "typed",
                    "message": {"role": "user", "content": "slug decoded typed prompt"},
                },
            ]
            (claude / "session.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )
            history_args = [
                "--sessions-root",
                str(grok_root),
                "--claude-root",
                str(history_root / "claude"),
            ]
            imported = invoke(history_root, "import-history", *history_args)
            history_items = json.loads((history_root / "staging.json").read_text())
            ok(
                imported.returncode == 0 and len(history_items) == 4,
                "history importer keeps only human-classified Grok and Claude prompts",
            )
            ok(
                sum(item["source"] == "grok-history" for item in history_items) == 2,
                "Grok bash and subagent-session records never stage",
            )
            ok(
                any(
                    item["source"] == "claude-history" and item["workspace"] == "/tmp/project"
                    for item in history_items
                ),
                "Claude cwd and source metadata are recorded",
            )
            ok(
                any(
                    item["prompt"] == "slug decoded typed prompt"
                    and item["workspace"] == "/tmp/project"
                    for item in history_items
                ),
                "Claude no-cwd slug decodes hyphens as path separators",
            )
            ok(
                all(
                    item["origin"] == "human-typed"
                    and item["origin_rule"] in {"human-provenance", "human-weak-textual"}
                    for item in history_items
                ),
                "history staging records provenance origin and rule",
            )
            ok(
                any(item["observed"]["intent"] == "implement" for item in history_items),
                "history uses today's static router verdict",
            )
            ok(
                "automation-metadata=" in imported.stdout
                and "automation-syntax=" in imported.stdout
                and "<command-name>" not in imported.stdout,
                "history report contains counts without prompt text",
            )
            ok(
                invoke(history_root, "import-history", *history_args).returncode == 0
                and len(json.loads((history_root / "staging.json").read_text())) == 4,
                "history hash dedup is idempotent",
            )
            ok(
                invoke(history_root, "harvest", *history_args).returncode == 0,
                "harvest preserves history watermark state",
            )
            ok(
                invoke(history_root, "import-history", *history_args).returncode == 0
                and len(json.loads((history_root / "staging.json").read_text())) == 4,
                "import-history after harvest replays nothing",
            )
            (grok_root / "plain workspace" / "prompt_history.jsonl").open(
                "a", encoding="utf-8"
            ).write(
                json.dumps(
                    {"timestamp": "2026-01-01T00:00:04Z", "prompt": "new appended history request"}
                )
                + "\n"
            )
            skipped = invoke(history_root, "import-history", *history_args)
            ok(
                skipped.returncode == 0
                and len(json.loads((history_root / "staging.json").read_text())) == 4
                and "uncertain-skipped=1" in skipped.stdout,
                "history file growth scans only its appended tail and skips uncertain prompts",
            )
            recovered = invoke(history_root, "import-history", *history_args, "--keep-uncertain")
            recovered_items = json.loads((history_root / "staging.json").read_text())
            ok(
                recovered.returncode == 0
                and any(
                    item["prompt"] == "new appended history request" for item in recovered_items
                ),
                "keep-uncertain recovers a prompt skipped by default policy",
            )
            ok(
                invoke(
                    history_root, "import-history", *history_args, "--workspaces", "рабочее место"
                ).returncode
                == 0,
                "decoded workspace filter is accepted",
            )
            ok(
                invoke(history_root, "import-history", *history_args, "--no-claude").returncode
                == 0,
                "no-claude skips Claude sources",
            )

        with tempfile.TemporaryDirectory() as history_regression_td:
            regression_root = Path(history_regression_td)
            claude_project = regression_root / "claude" / "-tmp-project"
            claude_project.mkdir(parents=True)
            claude_record = {
                "type": "user",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "message": {"role": "user", "content": "filtered slug workspace prompt"},
            }
            (claude_project / "session.jsonl").write_text(json.dumps(claude_record) + "\n")
            slug_filtered = invoke(
                regression_root,
                "import-history",
                "--workspaces",
                "/tmp/project",
            )
            slug_items = json.loads((regression_root / "staging.json").read_text())
            ok(
                slug_filtered.returncode == 0 and len(slug_items) == 1,
                "Claude decoded no-cwd workspace matches --workspaces",
            )

            workspace_a = regression_root / "workspace-a"
            workspace_b = regression_root / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            for workspace, prompt in (
                (workspace_a, "почини уникальный первый проект пожалуйста"),
                (workspace_b, "почини уникальный второй модуль пожалуйста"),
            ):
                history = regression_root / "sessions" / quote(str(workspace), safe="")
                history.mkdir(parents=True)
                (history / "prompt_history.jsonl").write_text(
                    json.dumps({"timestamp": "2026-01-01T00:00:00Z", "prompt": prompt}) + "\n"
                )
            narrow = invoke(
                regression_root,
                "import-history",
                "--no-claude",
                "--workspaces",
                str(workspace_a),
            )
            after_narrow = len(json.loads((regression_root / "staging.json").read_text()))
            widened = invoke(
                regression_root,
                "import-history",
                "--no-claude",
                "--workspaces",
                f"{workspace_a},{workspace_b}",
            )
            after_widen = json.loads((regression_root / "staging.json").read_text())
            ok(
                narrow.returncode == 0 and after_narrow == 2,
                "narrow workspace scan stages only its included workspace",
            )
            ok(
                widened.returncode == 0
                and len(after_widen) == 3
                and any(item["workspace"] == str(workspace_b) for item in after_widen),
                "widened workspace scan picks up newly included workspace",
            )

        with tempfile.TemporaryDirectory() as claude_limit_td:
            claude_limit_root = Path(claude_limit_td)
            claude_project = claude_limit_root / "claude" / "-tmp-project"
            claude_project.mkdir(parents=True)
            claude_records = [
                {
                    "type": "user",
                    "origin": {"kind": "human"},
                    "promptSource": "typed",
                    "message": {"role": "user", "content": prompt},
                }
                for prompt in (
                    "alpha migrate postgres indexes before deployment",
                    "bravo rotate kubernetes certificates across regions",
                    "charlie optimize image pipeline memory allocations",
                    "delta reconcile billing invoices with ledger exports",
                )
            ]
            claude_file = claude_project / "session.jsonl"
            claude_file.write_text(
                "".join(json.dumps(record) + "\n" for record in claude_records), encoding="utf-8"
            )
            limited_first = invoke(claude_limit_root, "import-history", "--limit", "2")
            limited_second = invoke(claude_limit_root, "import-history", "--limit", "2")
            limited_items = json.loads((claude_limit_root / "staging.json").read_text())
            limited_entry = json.loads((claude_limit_root / "watermark.json").read_text())[
                "history"
            ]["files"][str(claude_file)]
            ok(
                limited_first.returncode == 0
                and limited_second.returncode == 0
                and len(limited_items) == 4
                and any(
                    item["prompt"].startswith("charlie optimize image pipeline")
                    for item in limited_items
                )
                and set(limited_entry) == {"size", "mtime"},
                "Claude limited scans resume at the saved offset and prefix fingerprint",
            )

        with tempfile.TemporaryDirectory() as offset_td:
            offset_root = Path(offset_td)
            history = offset_root / "sessions" / "workspace"
            history.mkdir(parents=True)
            history_file = history / "prompt_history.jsonl"
            history_file.write_text(
                "".join(
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:00Z",
                            "prompt": f"generated command {index}",
                            "is_bash": True,
                        }
                    )
                    + "\n"
                    for index in range(8001)
                )
            )
            baseline = invoke(offset_root, "import-history", "--no-claude")
            baseline_items = json.loads((offset_root / "staging.json").read_text())
            with history_file.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:01Z",
                            "prompt": "почини только новую запись пожалуйста",
                        }
                    )
                    + "\n"
                )
            appended = invoke(offset_root, "import-history", "--no-claude")
            offset_items = json.loads((offset_root / "staging.json").read_text())
            ok(
                baseline.returncode == 0 and not baseline_items,
                "history offset baseline handles more than 8000 terminal records",
            )
            ok(
                appended.returncode == 0 and len(offset_items) == 1,
                "changed history file is fully rescanned and hash dedup retains the new candidate",
            )

        with tempfile.TemporaryDirectory() as partial_td:
            partial_root = Path(partial_td)
            grok_partial = partial_root / "sessions" / "workspace" / "prompt_history.jsonl"
            grok_partial.parent.mkdir(parents=True)
            grok_record = json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "prompt": "почини усеченную grok запись пожалуйста",
                }
            )
            grok_partial.write_text(grok_record, encoding="utf-8")
            first = invoke(partial_root, "import-history", "--no-claude")
            partial_state = json.loads((partial_root / "watermark.json").read_text())
            ok(
                first.returncode == 0
                and not json.loads((partial_root / "staging.json").read_text())
                and str(grok_partial) not in partial_state["history"]["files"],
                "Grok partial tail preserves its record-start retry offset",
            )
            grok_partial.open("a", encoding="utf-8").write("\n")
            second = invoke(partial_root, "import-history", "--no-claude")
            grok_items = json.loads((partial_root / "staging.json").read_text())
            ok(
                second.returncode == 0
                and len(grok_items) == 1
                and grok_items[0]["prompt"] == "почини усеченную grok запись пожалуйста",
                "completed Grok partial tail is processed exactly once",
            )

            claude_partial = partial_root / "claude" / "-tmp-project" / "session.jsonl"
            claude_partial.parent.mkdir(parents=True)
            claude_record = {
                "type": "user",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "message": {"role": "user", "content": "claude усеченная запись пожалуйста"},
            }
            claude_partial.write_text(json.dumps(claude_record), encoding="utf-8")
            third = invoke(partial_root, "import-history")
            claude_state = json.loads((partial_root / "watermark.json").read_text())
            ok(
                third.returncode == 0
                and not any(
                    item["source"] == "claude-history"
                    for item in json.loads((partial_root / "staging.json").read_text())
                )
                and str(claude_partial) not in claude_state["history"]["files"],
                "Claude partial tail preserves its record-start retry offset",
            )
            claude_partial.open("a", encoding="utf-8").write("\n")
            fourth = invoke(partial_root, "import-history")
            all_partial = json.loads((partial_root / "staging.json").read_text())
            ok(
                fourth.returncode == 0
                and sum(item["source"] == "claude-history" for item in all_partial) == 1,
                "completed Claude partial tail is processed exactly once",
            )

        with tempfile.TemporaryDirectory() as replacement_td:
            replacement_root = Path(replacement_td)
            replacement_history = (
                replacement_root / "sessions" / "workspace" / "prompt_history.jsonl"
            )
            replacement_history.parent.mkdir(parents=True)
            old_prompt = "почини хвост " + "а" * 80 + " альфа бета гамма дельта эпсилон"
            new_prompt = "почини хвост " + "а" * 80 + " зетта йота каппа лямбда омикрон"
            replacement_record = {"timestamp": "2026-01-01T00:00:00Z", "prompt": old_prompt}
            replacement_history.write_text(json.dumps(replacement_record) + "\n", encoding="utf-8")
            baseline = invoke(replacement_root, "import-history", "--no-claude")
            replacement_history.write_text(
                json.dumps({"timestamp": "2026-01-01T00:00:00Z", "prompt": new_prompt}) + "\n",
                encoding="utf-8",
            )
            replaced = invoke(replacement_root, "import-history", "--no-claude")
            replacement_items = json.loads((replacement_root / "staging.json").read_text())
            ok(
                baseline.returncode == 0
                and replaced.returncode == 0
                and any(item["prompt"] == new_prompt for item in replacement_items),
                "same-head same-size history replacement rescans changed tail content",
            )

        with tempfile.TemporaryDirectory() as middle_replacement_td:
            middle_root = Path(middle_replacement_td)
            middle_history = middle_root / "sessions" / "workspace" / "prompt_history.jsonl"
            middle_history.parent.mkdir(parents=True)
            old_middle = (
                "почини середину " + " ".join(f"a{index:02}" for index in range(60)) + " tail " * 60
            )
            new_middle = (
                "почини середину " + " ".join(f"b{index:02}" for index in range(60)) + " tail " * 60
            )
            middle_history.write_text(
                json.dumps(
                    {"timestamp": "2026-01-01T00:00:00Z", "prompt": old_middle}, ensure_ascii=False
                )
                + "\n",
                encoding="utf-8",
            )
            middle_baseline = invoke(
                middle_root, "import-history", "--no-claude", "--keep-uncertain"
            )
            middle_history.write_text(
                json.dumps(
                    {"timestamp": "2026-01-01T00:00:00Z", "prompt": new_middle}, ensure_ascii=False
                )
                + "\n",
                encoding="utf-8",
            )
            middle_replaced = invoke(
                middle_root, "import-history", "--no-claude", "--keep-uncertain"
            )
            middle_items = json.loads((middle_root / "staging.json").read_text())
            ok(
                middle_baseline.returncode == 0
                and middle_replaced.returncode == 0
                and any(
                    item["prompt"].startswith("почини середину b00 b01") for item in middle_items
                ),
                "same-head same-size middle replacement rescans the complete processed prefix",
            )

        with tempfile.TemporaryDirectory() as covered_td:
            covered_root = Path(covered_td)
            history = covered_root / "sessions" / "workspace"
            history.mkdir(parents=True)
            (history / "prompt_history.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "prompt": "почини красный тест пожалуйста",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "prompt": "почини красный тест пожалуйста сейчас",
                    }
                )
                + "\n"
            )
            original_staging = [
                {"id": "ms-1", "prompt": "почини красный тест пожалуйста", "expect": None}
            ]
            write_json(covered_root / "staging.json", original_staging)
            covered = invoke(covered_root, "import-history", "--no-claude")
            ok(
                covered.returncode == 0 and "covered=2" in covered.stdout,
                "exact and near duplicates against staging count as covered",
            )
            ok(
                json.loads((covered_root / "staging.json").read_text()) == original_staging,
                "covered staging duplicates do not append candidates",
            )

        with tempfile.TemporaryDirectory() as file_cap_td:
            file_cap_root = Path(file_cap_td)
            for index in range(501):
                history_file = (
                    file_cap_root / "sessions" / f"workspace-{index}" / "prompt_history.jsonl"
                )
                history_file.parent.mkdir(parents=True)
                history_file.write_text("", encoding="utf-8")
            capped = invoke(file_cap_root, "import-history", "--no-claude")
            capped_files = json.loads((file_cap_root / "watermark.json").read_text())["history"][
                "files"
            ]
            ok(
                capped.returncode == 0 and len(capped_files) == 500,
                "history watermark caps tracked files at 500 entries",
            )

        with tempfile.TemporaryDirectory() as policy_td:
            policy_root = Path(policy_td)
            plain_ws = policy_root / "plain-workspace"
            plain_ws.mkdir()
            plain_history = policy_root / "sessions" / quote(str(plain_ws), safe="")
            plain_history.mkdir(parents=True)
            (plain_history / "prompt_history.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "prompt": "investigate a subtle failure in this project",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "prompt": "You are the implement-standard station for this project",
                    }
                )
                + "\n"
            )
            skipped = invoke(policy_root, "import-history")
            skipped_hashes = json.loads((policy_root / "watermark.json").read_text())["history"][
                "prompt_hashes"
            ]
            ok(
                skipped.returncode == 0
                and not json.loads((policy_root / "staging.json").read_text())
                and corpus_sync._history_hash("investigate a subtle failure in this project")
                not in skipped_hashes,
                "automation is never staged and uncertain history without git evidence is skipped without watermarking its digest",
            )

            digest_root = policy_root / "digest-order"
            digest_ws = digest_root / "workspace"
            digest_ws.mkdir(parents=True)
            digest_history = digest_root / "sessions" / quote(str(digest_ws), safe="")
            digest_history.mkdir(parents=True)
            identical_prompt = "investigate an identical uncertain failure"
            (digest_history / "prompt_history.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "prompt": identical_prompt,
                        "is_bash": True,
                    }
                )
                + "\n"
                + json.dumps({"timestamp": "2026-01-01T00:00:01Z", "prompt": identical_prompt})
                + "\n"
            )
            digest_skipped = invoke(digest_root, "import-history")
            digest_kept = invoke(digest_root, "import-history", "--keep-uncertain")
            digest_items = json.loads((digest_root / "staging.json").read_text())
            ok(
                digest_skipped.returncode == 0 and digest_kept.returncode == 0 and not digest_items,
                "prompt hash dedup applies across history provenance",
            )

            keep_root = policy_root / "keep"
            keep_ws = keep_root / "workspace"
            keep_ws.mkdir(parents=True)
            keep_history = keep_root / "sessions" / quote(str(keep_ws), safe="")
            keep_history.mkdir(parents=True)
            (keep_history / "prompt_history.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "prompt": "investigate another subtle failure in this project",
                    }
                )
                + "\n"
            )
            kept = invoke(keep_root, "import-history", "--keep-uncertain")
            kept_item = json.loads((keep_root / "staging.json").read_text())[0]
            kept_hashes = json.loads((keep_root / "watermark.json").read_text())["history"][
                "prompt_hashes"
            ]
            ok(
                kept.returncode == 0
                and kept_item["flag"] == "uncertain-origin"
                and kept_item["git_evidence"] is None
                and corpus_sync._history_hash("investigate another subtle failure in this project")
                in kept_hashes,
                "keep-uncertain stages uncertain history and records its watermark digest",
            )

            evidence_root = policy_root / "evidence"
            evidence_ws = evidence_root / "workspace"
            evidence_ws.mkdir(parents=True)
            subprocess.run(["git", "-C", str(evidence_ws), "init", "-q"], check=True)
            (evidence_ws / "fix").write_text("fixed")
            subprocess.run(["git", "-C", str(evidence_ws), "add", "fix"], check=True)
            commit_env = {
                **os.environ,
                "GIT_AUTHOR_DATE": "2026-01-01T12:00:00Z",
                "GIT_COMMITTER_DATE": "2026-01-01T12:00:00Z",
            }
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(evidence_ws),
                    "-c",
                    "user.email=a@b",
                    "-c",
                    "user.name=t",
                    "commit",
                    "-qm",
                    "fix the real failure",
                ],
                env=commit_env,
                check=True,
            )
            evidence_history = evidence_root / "sessions" / quote(str(evidence_ws), safe="")
            evidence_history.mkdir(parents=True)
            (evidence_history / "prompt_history.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "prompt": "investigate the real failure in this project",
                    }
                )
                + "\n"
            )
            evidenced = invoke(evidence_root, "import-history")
            evidenced_item = json.loads((evidence_root / "staging.json").read_text())[0]
            ok(
                evidenced.returncode == 0
                and evidenced_item["flag"] == "uncertain-origin+git"
                and evidenced_item["git_evidence"]["subject"] == "fix the real failure",
                "uncertain history with a commit in the evidence window is staged",
            )

        with tempfile.TemporaryDirectory() as git_td:
            git_root = Path(git_td)
            repo = git_root / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            for index, subject in enumerate(
                ("Merge branch 'topic'", "bump version", "Fix cache invalidation race"), start=1
            ):
                (repo / f"file-{index}").write_text(str(index))
                subprocess.run(["git", "-C", str(repo), "add", f"file-{index}"], check=True)
                env = {
                    **os.environ,
                    "GIT_AUTHOR_DATE": f"2026-01-0{index}T12:00:00Z",
                    "GIT_COMMITTER_DATE": f"2026-01-0{index}T12:00:00Z",
                }
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "-c",
                        "user.email=a@b",
                        "-c",
                        "user.name=t",
                        "commit",
                        "-qm",
                        subject,
                    ],
                    env=env,
                    check=True,
                )
            imported_git = invoke(
                git_root,
                "import-git",
                "--workspaces",
                str(repo),
                "--since",
                "2025-01-01",
            )
            git_items = json.loads((git_root / "staging.json").read_text())
            ok(
                imported_git.returncode == 0
                and len(git_items) == 1
                and git_items[0]["prompt"] == "Fix cache invalidation race",
                "import-git skips merge and bump chores and stages the real fix",
            )
            ok(
                git_items[0]["origin"] == "git-commit" and git_items[0]["flag"] == "git-derived",
                "import-git records guaranteed-real provenance",
            )
            git_state = json.loads((git_root / "watermark.json").read_text())["history"][
                "git_commits"
            ]
            ok(
                [str(repo), git_items[0]["git_evidence"]["sha"]] in git_state,
                "import-git watermarks workspace and sha",
            )
            repeated_git = invoke(
                git_root,
                "import-git",
                "--workspaces",
                str(repo),
                "--since",
                "2025-01-01",
            )
            ok(
                repeated_git.returncode == 0
                and len(json.loads((git_root / "staging.json").read_text())) == 1
                and "staged=0" in repeated_git.stdout,
                "import-git is idempotent on rerun",
            )

        with tempfile.TemporaryDirectory() as git_cap_td:
            cap_root = Path(git_cap_td)
            cap_repos = []
            for index, subject in enumerate(("Fix alpha budget", "Build omega budget")):
                cap_repo = cap_root / f"repo-{index}"
                cap_repo.mkdir()
                subprocess.run(["git", "-C", str(cap_repo), "init", "-q"], check=True)
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(cap_repo),
                        "-c",
                        "user.email=a@b",
                        "-c",
                        "user.name=t",
                        "commit",
                        "--allow-empty",
                        "-qm",
                        subject,
                    ],
                    check=True,
                )
                cap_repos.append(cap_repo)
            capped = invoke(
                cap_root,
                "import-git",
                "--workspaces",
                ",".join(map(str, cap_repos)),
                "--since",
                "2025-01-01",
                "--cap",
                "1",
            )
            ok(
                capped.returncode == 0
                and len(json.loads((cap_root / "staging.json").read_text())) == 1,
                "import-git cap is shared across workspaces",
            )

        with tempfile.TemporaryDirectory() as git_clamp_td:
            clamp_root = Path(git_clamp_td)
            clamp_repo = clamp_root / "repo"
            clamp_repo.mkdir()
            subprocess.run(["git", "-C", str(clamp_repo), "init", "-q"], check=True)
            for index in range(201):
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(clamp_repo),
                        "-c",
                        "user.email=a@b",
                        "-c",
                        "user.name=t",
                        "commit",
                        "--allow-empty",
                        "-qm",
                        f"Implement feature token{index:03d}",
                    ],
                    check=True,
                )
            clamped = invoke(
                clamp_root,
                "import-git",
                "--workspaces",
                str(clamp_repo),
                "--since",
                "2025-01-01",
                "--cap",
                "500",
            )
            ok(
                clamped.returncode == 0
                and len(json.loads((clamp_root / "staging.json").read_text())) == 200,
                "import-git clamps cap 500 to 200",
            )

        # Triage prioritizes drift before other flags and preserves input order on ties.
        triage_items = [
            {"id": "negative", "flag": "negative-candidate", "prompt": "negative prompt"},
            {"id": "drift", "flag": "drift:rank", "prompt": "drift prompt"},
            {"id": "git", "flag": "git-derived", "prompt": "git prompt"},
        ]
        write_json(root / "staging.json", triage_items)
        triaged = invoke(root, "triage", "--limit", "1")
        first_entry = next(line for line in triaged.stdout.splitlines() if line.startswith("id: "))
        ok(
            triaged.returncode == 0 and first_entry.startswith("id: drift flag: drift:rank"),
            "triage prints highest-priority flag first",
        )

        # Plain directories produce unknown git state; a committed change is true.
        gitdir = root / "git-work"
        gitdir.mkdir()
        (gitdir / "x").write_text("x")
        subprocess.run(["git", "-C", str(gitdir), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(gitdir), "-c", "user.email=a@b", "-c", "user.name=t", "add", "x"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(gitdir),
                "-c",
                "user.email=a@b",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "initial",
            ],
            check=True,
        )
        ok(
            corpus_sync.git_check(str(gitdir), "2000-01-01T00:00:00Z") is True,
            "git check sees committed changes",
        )
        ok(
            corpus_sync.git_check(str(root / "plain"), "2000-01-01T00:00:00Z") is None,
            "plain directory has unknown git state",
        )
    print("OK: corpus-sync tests complete")
    return


def main() -> int:
    test_classify_provenance()
    test_corpus_sync()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError:
        raise SystemExit(1)


def test_xdg_state_paths_and_staging_migration(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    assert corpus_sync.state_dir() == (xdg / "grok-route").resolve()
    assert corpus_sync.sidecar_path("route.jsonl") == corpus_sync.state_dir() / "route.jsonl"
    assert corpus_sync.sidecar_path("corpus-sync.json").parent == corpus_sync.state_dir()
    staging = corpus_sync.sidecar_path("workloads-staging.json")
    legacy = tmp_path / "legacy.json"
    write_json(legacy, [{"id": "legacy"}])
    monkeypatch.setattr(corpus_sync, "OLD_STAGING", legacy)
    corpus_sync.migrate_staging(staging)
    assert json.loads(staging.read_text()) == [{"id": "legacy"}]
    assert staging.is_relative_to(xdg)
    monkeypatch.delenv("XDG_STATE_HOME")
    assert corpus_sync.state_dir() == (Path.home() / ".local" / "state" / "grok-route").resolve()


def test_known_roles_lazy(monkeypatch):
    import importlib

    import grokbuild.roles as roles

    expected = (
        roles.load_registry().names() | set(roles.ROLE_ALIASES) | set(roles.IMPLEMENT_ROLE_NAMES)
    ) - {
        "consilium-analyst",
        "consilium-arbiter",
        "consilium-challenger",
        "explore-risk",
    }
    assert not hasattr(corpus_sync, "KNOWN_ROLES"), "role universe must not load at import"
    assert corpus_sync._known_roles() == expected

    def _boom(*args, **kwargs):
        raise AssertionError("load_registry must not run at import time")

    real = corpus_sync.load_registry
    monkeypatch.setattr(roles, "load_registry", _boom)
    importlib.reload(corpus_sync)
    assert not hasattr(corpus_sync, "KNOWN_ROLES")
    monkeypatch.undo()
    corpus_sync.load_registry = real
    assert corpus_sync._known_roles() == expected
