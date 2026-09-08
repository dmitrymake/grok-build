from __future__ import annotations

import json
import sys

from _harness import bootstrap

bootstrap()
from grokbuild import hook
from grokbuild import hook as hook_route


def test_main_pre_tool_telemetry_failure_denies(monkeypatch):
    monkeypatch.setattr(
        hook,
        "_append_log",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("telemetry")),
    )
    old = sys.stdin
    import io
    import contextlib

    class Stdin:
        def read(self):
            return '{"hookEventName":"PreToolUse","toolName":"search_replace","toolInput":{}}'

    buf = io.StringIO()
    try:
        sys.stdin = Stdin()
        with contextlib.redirect_stdout(buf):
            try:
                rc = hook_route.main()
            except SystemExit as exc:
                rc = int(exc.code or 0)
    finally:
        sys.stdin = old
    assert rc == 2
    assert json.loads(buf.getvalue())["decision"] == "deny"
