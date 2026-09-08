#!/usr/bin/env python3
"""Deterministic visual-intake contract and coordinator tests."""

from __future__ import annotations

from _harness import run_standalone

from _harness import make_check

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()
ROOT = REPO_ROOT / "grokbuild"
import hashlib
import math
import os
import tempfile

from grokbuild.roles import ROLE_ALIASES, Capabilities, ProviderModelMeta, Role, RoleRegistry
from grokbuild.visual_intake import (
    AttachmentRef,
    VisualIntakeErrorV1,
    VisualIntakeHandoff,
    VisualIntakePolicy,
    VisualIntakeRequestV1,
    compact_visual_context,
    conductor_provider_payload,
    request_visual_analysis,
    resolve_visual_role,
    run_visual_intake,
    should_use_deep_intake,
    validate_visual_intake_result,
)

FAILURES = []


check = make_check(FAILURES)


def attachment(i="a", content=b"x", mime="image/png"):
    return AttachmentRef(i, hashlib.sha256(content).hexdigest(), mime)


def payload(ids=("a",), confidence=0.9, deeper=False, image_type="ui_screenshot", uncertainties=()):
    return {
        "schema_version": 1,
        "role_version": "1",
        "attachment_ids": list(ids),
        "image_type": image_type,
        "summary": "screen",
        "visible_text": "Error",
        "key_elements": [
            {"type": "button", "text": "OK", "description": "action", "location": "bottom"}
        ],
        "observations": [
            {"kind": "fact", "text": "error shown", "attachment_ids": list(ids)},
            {"kind": "inference", "text": "operation failed"},
        ],
        "likely_relevance": ["debugging"],
        "uncertainties": list(uncertainties),
        "needs_deeper_visual_analysis": deeper,
        "confidence": confidence,
    }


def registry(vision=True, fallback=True):
    roles = {
        "visual-intake": Role(
            "visual-intake",
            "primary",
            capability_mode="read-only",
            write=False,
            fallback=("visual-intake-deep",) if fallback else (),
            capabilities=Capabilities(vision=vision),
            provider="p",
        ),
        "visual-intake-deep": Role(
            "visual-intake-deep",
            "deep",
            capability_mode="read-only",
            write=False,
            capabilities=Capabilities(vision=True),
            provider="p",
        ),
    }
    metas = {
        m: ProviderModelMeta("p", "p", None, None, None, None, capabilities=r.capabilities)
        for m, r in ((x.model, x) for x in roles.values())
    }
    return RoleRegistry(roles, provider_catalog=metas)


class Invoker:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def expect_bad(data, msg):
    try:
        validate_visual_intake_result(data)
        check(False, msg)
    except (ValueError, TypeError):
        check(True, msg)


def test_visual_intake():
    good = validate_visual_intake_result(payload())
    check(validate_visual_intake_result(good.to_dict()) == good, "valid round trip")
    for key, value, msg in (
        ("schema_version", 2, "unknown version"),
        ("confidence", 2, "confidence bounds"),
        ("confidence", math.nan, "confidence NaN"),
        ("confidence", math.inf, "confidence infinity"),
    ):
        d = payload()
        d[key] = value
        expect_bad(d, msg)
    d = payload()
    d.pop("summary")
    expect_bad(d, "missing required field")
    d = payload()
    d["extra"] = 1
    expect_bad(d, "unknown field")
    d = payload()
    d["observations"] = [{"text": "x"}]
    expect_bad(d, "observation kind required")
    d = payload()
    d["observations"] = [{"kind": "fact", "text": "x", "attachment_ids": ["other"]}]
    expect_bad(d, "observation attachment ids subset")
    for opts, msg in (
        ({"confidence": 0.4}, "low confidence uncertainty"),
        ({"deeper": True}, "deep uncertainty"),
        ({"image_type": "unknown"}, "unknown type uncertainty"),
    ):
        expect_bad(payload(**opts), msg)
    d = payload()
    d["visible_text"] = "x" * 20001
    expect_bad(d, "visible text bound")
    err = VisualIntakeErrorV1(1, ("a",), "unsupported_media", "Unsupported attachment.", False)
    check(err.code == "unsupported_media", "typed unsupported media")
    text = compact_visual_context("fix this", good, ("a",))
    check(
        all(x in text for x in ("USER REQUEST", "Facts:", "Inferences:", "UNCERTAINTIES"))
        and not text.lstrip().startswith("{"),
        "compact non-JSON serialization",
    )
    os.environ.setdefault("MINIMAX_API_KEY", "test")
    os.environ.setdefault("CODEX_SUB_PROXY_BEARER", "test")
    with tempfile.TemporaryDirectory() as raw:
        cache = Path(raw) / "cache.json"
        reg = registry()
        req = VisualIntakeRequestV1("text", (), VisualIntakePolicy())
        inv = Invoker([])
        out = run_visual_intake(req, registry=reg, invoker=inv, cache_path=cache)
        check(not inv.calls and not out.conductor_received_visual_context, "text-only auto skips")
        req = VisualIntakeRequestV1(
            "inspect this image carefully", (attachment(),), VisualIntakePolicy()
        )
        inv = Invoker([payload()])
        out = run_visual_intake(req, registry=reg, invoker=inv, cache_path=cache, prefer_deep=False)
        check(
            len(inv.calls) == 1
            and out.result
            and out.conductor_received_visual_context
            and out.retained_attachment_ids == ("a",),
            "single screenshot coordinator",
        )
        req2 = VisualIntakeRequestV1("", (attachment("b", b"b"),), VisualIntakePolicy())
        out = run_visual_intake(
            req2,
            registry=reg,
            invoker=Invoker([payload(("b",))]),
            cache_path=cache,
            prefer_deep=False,
        )
        check("(empty)" in out.compact_context, "image-only context usable")
        req3 = VisualIntakeRequestV1(
            "compare", (attachment("a"), attachment("b", b"b")), VisualIntakePolicy(), compare=True
        )
        out = run_visual_intake(
            req3,
            registry=reg,
            invoker=Invoker([payload(("a", "b"))]),
            cache_path=cache,
            prefer_deep=False,
        )
        check(out.result and out.result.attachment_ids == ("a", "b"), "multi-image correspondence")
        inv = Invoker([RuntimeError(), payload()])
        out = run_visual_intake(
            req, registry=reg, invoker=inv, cache_path=Path(raw) / "f", prefer_deep=False
        )
        check(out.used_fallback and out.role == "visual-intake-deep", "primary failure falls back")
        out = run_visual_intake(
            req,
            registry=reg,
            invoker=Invoker([RuntimeError(), RuntimeError()]),
            cache_path=Path(raw) / "all",
            prefer_deep=False,
        )
        check(
            out.error
            and "unavailable" in out.compact_context.casefold()
            and out.conductor_received_visual_context,
            "all failures produce marker",
        )
        missing = "VISUAL_TEST_MISSING_CREDENTIAL"
        os.environ.pop(missing, None)
        roles = {
            "visual-intake": reg.get("visual-intake"),
            "visual-intake-deep": reg.get("visual-intake-deep"),
        }
        metas = dict(reg.provider_catalog)
        metas["deep"] = ProviderModelMeta(
            "p",
            "p",
            None,
            None,
            None,
            None,
            credential_env=missing,
            capabilities=Capabilities(vision=True),
        )
        ineligible = RoleRegistry(roles, provider_catalog=metas)
        inv = Invoker([RuntimeError()])
        out = run_visual_intake(
            req,
            registry=ineligible,
            invoker=inv,
            cache_path=Path(raw) / "ineligible",
            prefer_deep=False,
        )
        check(
            len(inv.calls) == 1 and out.error and not out.used_fallback,
            "ineligible fallback is not invoked",
        )
        low_payload = payload(confidence=0.2, uncertainties=("small text",))
        inv = Invoker([low_payload, payload()])
        out = run_visual_intake(
            req, registry=reg, invoker=inv, cache_path=Path(raw) / "low-deep", prefer_deep=False
        )
        check(
            [call["role"] for call in inv.calls] == ["visual-intake", "visual-intake-deep"]
            and out.role == "visual-intake-deep"
            and out.used_fallback,
            "low confidence invokes deep role",
        )
        deep_payload = payload(deeper=True, uncertainties=("detail needed",))
        inv = Invoker([deep_payload, payload()])
        out = run_visual_intake(
            req, registry=reg, invoker=inv, cache_path=Path(raw) / "needs-deep", prefer_deep=False
        )
        check(
            len(inv.calls) == 2 and out.role == "visual-intake-deep",
            "needs-deeper flag invokes deep role",
        )
        inv = Invoker([low_payload, RuntimeError()])
        out = run_visual_intake(
            req, registry=reg, invoker=inv, cache_path=Path(raw) / "deep-fails", prefer_deep=False
        )
        check(
            out.result and out.result.confidence == 0.2 and out.conductor_received_visual_context,
            "deep failure returns valid primary result",
        )
        bad = payload()
        bad["schema_version"] = 9
        inv = Invoker([bad, bad, payload()])
        out = run_visual_intake(
            req, registry=reg, invoker=inv, cache_path=Path(raw) / "repair", prefer_deep=False
        )
        check(
            out.used_fallback and len(inv.calls) == 3, "invalid schema repaired once then fallback"
        )
        low = validate_visual_intake_result(payload(confidence=0.4, uncertainties=("small text",)))
        check(should_use_deep_intake(req, low), "low confidence suggests deep intake")
        reout = request_visual_analysis(
            ("a",),
            "error",
            "high",
            attachments={"a": attachment()},
            registry=reg,
            invoker=Invoker([payload()]),
            cache_path=Path(raw) / "rean",
        )
        check(reout.result is not None, "request_visual_analysis reinspects")
        check(
            run_visual_intake(
                VisualIntakeRequestV1("x", (attachment(),), VisualIntakePolicy(mode="off")),
                registry=reg,
                invoker=Invoker([]),
                cache_path=cache,
            ).telemetry["visual_intake_invoked"]
            is False,
            "mode off no invoke",
        )
        inv = Invoker([])
        run_visual_intake(
            VisualIntakeRequestV1("x", (), VisualIntakePolicy(mode="always")),
            registry=reg,
            invoker=inv,
            cache_path=cache,
        )
        check(not inv.calls, "always text-only no model")
        for mime in ("application/pdf", "image/svg+xml", "image/heic", "image/tiff", "text/plain"):
            inv = Invoker([])
            out = run_visual_intake(
                VisualIntakeRequestV1("inspect", (attachment(mime=mime),), VisualIntakePolicy()),
                registry=reg,
                invoker=inv,
                cache_path=Path(raw) / (mime.replace("/", "-")),
                prefer_deep=False,
            )
            check(
                out.error and out.error.code == "unsupported_media" and not inv.calls,
                f"unsupported MIME {mime}",
            )
        param = attachment(mime=" Image/PNG; charset=binary ")
        inv = Invoker([payload()])
        out = run_visual_intake(
            VisualIntakeRequestV1("inspect this image carefully", (param,), VisualIntakePolicy()),
            registry=reg,
            invoker=inv,
            cache_path=Path(raw) / "params",
            prefer_deep=False,
        )
        check(
            out.result is not None and len(inv.calls) == 1, "supported MIME parameters normalized"
        )
        mixed = (attachment("ok", b"ok"), attachment("pdf", b"pdf", "application/pdf"))
        inv = Invoker([payload(("ok",))])
        out = run_visual_intake(
            VisualIntakeRequestV1("inspect this image carefully", mixed, VisualIntakePolicy()),
            registry=reg,
            invoker=inv,
            cache_path=Path(raw) / "mixed",
            prefer_deep=False,
        )
        check(
            out.result
            and tuple(a.attachment_id for a in inv.calls[0]["request"].attachments) == ("ok",),
            "mixed media sends only supported attachments",
        )
    turn = {
        "content": [
            {"type": "text", "text": "x"},
            {
                "type": "image_url",
                "attachment_id": "a",
                "image_url": {"url": "data:image/png;base64,xx"},
            },
        ]
    }
    wire = conductor_provider_payload(turn, False)
    check(
        "image_url" not in str(wire) and wire["canonical_attachments"] == ["a"],
        "nonvision payload strips image and retains id",
    )
    check(resolve_visual_role(registry(False), prefer_deep=False) is None, "vision false rejected")
    named = RoleRegistry(
        {
            "visual-intake": Role(
                "visual-intake", "super-vision-model", capabilities=Capabilities(vision=False)
            )
        },
        provider_catalog={
            "super-vision-model": ProviderModelMeta(
                "p", "p", None, None, None, None, capabilities=Capabilities(vision=False)
            )
        },
    )
    check(
        resolve_visual_role(named) is None and "visual-intake" not in ROLE_ALIASES,
        "model name never infers vision and aliases unchanged",
    )
    if FAILURES:
        print(f"{len(FAILURES)} failures")
        return
    print("visual intake tests passed")
    return


def test_handoff_roundtrip():
    result = validate_visual_intake_result(payload())
    handoff = VisualIntakeHandoff(
        "compact context",
        result,
        None,
        {"role": "visual-intake"},
        True,
        ("a",),
        False,
        False,
        "visual-intake",
        "super-vision-model",
    )
    check(VisualIntakeHandoff.from_dict(handoff.to_dict()) == handoff, "handoff result round trip")
    failure = VisualIntakeHandoff(
        "",
        None,
        VisualIntakeErrorV1(1, ("a",), "unsupported_media", "no image bytes", False),
        {},
        False,
        (),
        True,
        False,
        None,
        None,
    )
    check(VisualIntakeHandoff.from_dict(failure.to_dict()) == failure, "handoff error round trip")


def main():
    test_visual_intake()
    test_handoff_roundtrip()
    return 1 if FAILURES else 0


if __name__ == "__main__":
    run_standalone(main)
