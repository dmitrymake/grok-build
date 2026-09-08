#!/usr/bin/env python3
"""Deterministic visual-intake cache tests."""

from __future__ import annotations

from _harness import run_standalone

from _harness import make_check

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()
ROOT = REPO_ROOT / "grokbuild"
import hashlib
import json
import stat
import tempfile

from grokbuild.roles import Capabilities, ProviderModelMeta, Role, RoleRegistry
from grokbuild.visual_cache import TTL, visual_cache_get, visual_cache_put
from grokbuild.visual_intake import (
    AttachmentRef,
    VisualIntakePolicy,
    VisualIntakeRequestV1,
    run_visual_intake,
    visual_cache_key,
)

FAILURES = []


check = make_check(FAILURES)


def test_visual_cache():
    with tempfile.TemporaryDirectory() as raw:
        p = Path(raw) / "cache.json"

        def h(b):
            return hashlib.sha256(b).hexdigest()

        a = AttachmentRef("a", h(b"same"), "image/png")
        renamed = AttachmentRef("other", h(b"same"), "image/png")
        changed = AttachmentRef("a", h(b"changed"), "image/png")
        opts = {"detail": "auto"}
        b = "binding"
        k = visual_cache_key((a.content_hash,), 1, "1", opts, b)
        check(
            k == visual_cache_key((renamed.content_hash,), 1, "1", opts, b),
            "same bytes ignore filename/id",
        )
        check(
            k != visual_cache_key((changed.content_hash,), 1, "1", opts, b),
            "changed bytes change key",
        )
        check(
            len(
                {
                    k,
                    visual_cache_key((a.content_hash,), 2, "1", opts, b),
                    visual_cache_key((a.content_hash,), 1, "2", opts, b),
                    visual_cache_key((a.content_hash,), 1, "1", {"detail": "high"}, b),
                    visual_cache_key((a.content_hash,), 1, "1", opts, "new"),
                }
            )
            == 5,
            "schema role options binding invalidate",
        )
        check(
            visual_cache_key((h(b"1"), h(b"2")), 1, "1", opts, b)
            != visual_cache_key((h(b"2"), h(b"1")), 1, "1", opts, b),
            "order matters",
        )
        visual_cache_put(k, {"kind": "result", "value": {"summary": "safe"}}, p, now=1)
        check(visual_cache_get(k, p, now=2) is not None, "cache hit")
        check(visual_cache_get(k, p, now=TTL + 2) is None, "expired miss")
        p.write_text("broken", encoding="utf-8")
        check(visual_cache_get(k, p) is None, "corrupt miss")
        visual_cache_put(k, {"kind": "result", "value": {"summary": "safe"}}, p)
        text = p.read_text()
        check(
            "data:image" not in text and "/home/" not in text and "filename" not in text,
            "cache contains no image path or filename",
        )
        check(stat.S_IMODE(p.stat().st_mode) == 0o600, "cache mode 0600")

        # Transient errors are coordinator misses and are never written.
        class Fail:
            def invoke(self, **kw):
                raise RuntimeError("down")

        reg = RoleRegistry(
            {
                "visual-intake": Role(
                    "visual-intake",
                    "m",
                    capability_mode="read-only",
                    write=False,
                    capabilities=Capabilities(vision=True),
                )
            },
            provider_catalog={
                "m": ProviderModelMeta(
                    "p", "p", None, None, None, None, capabilities=Capabilities(vision=True)
                )
            },
        )
        req = VisualIntakeRequestV1("inspect", (a,), VisualIntakePolicy())
        run_visual_intake(
            req, registry=reg, invoker=Fail(), cache_path=Path(raw) / "transient", prefer_deep=False
        )
        check(not (Path(raw) / "transient").exists(), "transient failure not cached")
        good = {
            "schema_version": 1,
            "role_version": "1",
            "attachment_ids": ["a"],
            "image_type": "photo",
            "summary": "safe",
            "visible_text": "",
            "key_elements": [],
            "observations": [],
            "likely_relevance": [],
            "uncertainties": [],
            "needs_deeper_visual_analysis": False,
            "confidence": 1.0,
        }

        class Once:
            def __init__(self):
                self.calls = 0

            def invoke(self, **kw):
                self.calls += 1
                return good

        inv = Once()
        cp = Path(raw) / "coordinator"
        run_visual_intake(req, registry=reg, invoker=inv, cache_path=cp, prefer_deep=False)
        renamed_req = VisualIntakeRequestV1("inspect", (renamed,), VisualIntakePolicy())
        second = run_visual_intake(
            renamed_req, registry=reg, invoker=inv, cache_path=cp, prefer_deep=False
        )
        check(inv.calls == 1 and second.cached, "same bytes with renamed id hits cache")
        check(
            second.result
            and second.result.attachment_ids == ("other",)
            and "other" in second.compact_context
            and "\na\n" not in second.compact_context
            and second.retained_attachment_ids == ("other",),
            "cache hit remaps current attachment id",
        )
        secret_good = {
            **good,
            "summary": "visible OCR summary",
            "visible_text": "sk-secret-api-key-12345",
            "key_elements": [
                {
                    "type": "token",
                    "text": "key element secret",
                    "description": "credential",
                    "location": None,
                }
            ],
            "observations": [
                {"kind": "fact", "text": "observation secret", "attachment_ids": ["a"]}
            ],
        }
        secret_inv = Once()
        secret_inv.invoke = lambda **kw: secret_good
        secret_cache = Path(raw) / "secret-cache"
        secret_cache.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        "old": {"created_at": 1, "value": {"visible_text": "sk-old-secret"}}
                    },
                }
            ),
            encoding="utf-8",
        )
        run_visual_intake(
            req, registry=reg, invoker=secret_inv, cache_path=secret_cache, prefer_deep=False
        )
        serialized = secret_cache.read_text(encoding="utf-8")
        check(
            all(
                value not in serialized
                for value in (
                    "sk-",
                    "secret-api-key-12345",
                    "visible OCR summary",
                    "observation secret",
                    "key element secret",
                )
            ),
            "cache persists no OCR or extracted secrets",
        )
        first = (
            AttachmentRef("old-1", h(b"1"), "image/png"),
            AttachmentRef("old-2", h(b"2"), "image/png"),
        )
        multi_good = {**good, "attachment_ids": ["old-1", "old-2"]}

        class Multi:
            def __init__(self):
                self.calls = 0

            def invoke(self, **kw):
                self.calls += 1
                return multi_good

        multi = Multi()
        multi_cache = Path(raw) / "multi"
        run_visual_intake(
            VisualIntakeRequestV1("inspect carefully please", first, VisualIntakePolicy()),
            registry=reg,
            invoker=multi,
            cache_path=multi_cache,
            prefer_deep=False,
        )
        current = (
            AttachmentRef("new-1", h(b"1"), "image/png"),
            AttachmentRef("new-2", h(b"2"), "image/png"),
        )
        multi_hit = run_visual_intake(
            VisualIntakeRequestV1("inspect carefully please", current, VisualIntakePolicy()),
            registry=reg,
            invoker=multi,
            cache_path=multi_cache,
            prefer_deep=False,
        )
        check(
            multi.calls == 1
            and multi_hit.cached
            and multi_hit.result
            and multi_hit.result.attachment_ids == ("new-1", "new-2"),
            "multi-image cache remap preserves order",
        )
    if FAILURES:
        print(f"{len(FAILURES)} failures")
        return
    print("visual cache tests passed")
    return


def main():
    test_visual_cache()
    return 1 if FAILURES else 0


if __name__ == "__main__":
    run_standalone(main)


def test_version_mismatch_is_non_destructive_and_unsupported_media_is_not_cached(tmp_path):
    path = tmp_path / "cache.json"
    original = '{"version": 999, "entries": {"k": {"created_at": 1, "value": {}}}}'
    path.write_text(original, encoding="utf-8")
    assert visual_cache_get("k", path, now=2) is None
    assert path.read_text(encoding="utf-8") == original
    request = VisualIntakeRequestV1(
        "inspect", (AttachmentRef("audio", "a" * 64, "audio/wav"),), VisualIntakePolicy()
    )
    result = run_visual_intake(request, registry=None, invoker=None, cache_path=path)
    assert result.error and result.error.code == "unsupported_media"
    assert path.read_text(encoding="utf-8") == original
