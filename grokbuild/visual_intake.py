#!/usr/bin/env python3
"""Typed visual-intake contract and runtime-owned invocation coordinator."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from grokbuild.roles import RoleRegistry, credential_present
from grokbuild.visual_cache import visual_cache_get, visual_cache_put

IMAGE_TYPES = frozenset(
    {"ui_screenshot", "document", "photo", "diagram", "chart", "code_screenshot", "unknown"}
)
SCHEMA_VERSION = 1
ROLE_VERSION = "1"
LOW_CONFIDENCE_THRESHOLD = 0.5
SUPPORTED_IMAGE_MIMES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
        "image/gif",
    }
)
_OBSERVATION_KINDS = frozenset({"fact", "inference"})
_ERROR_CODES = frozenset(
    {
        "unsupported_media",
        "binding_unavailable",
        "analysis_failed",
        "invalid_output",
        "policy_disabled",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load_intent_markers() -> tuple[str, ...]:
    try:
        data = (
            json.loads(
                (Path(__file__).resolve().parent / "intents.json").read_text(encoding="utf-8")
            )
            .get("markers", {})
            .get("vision_triggers")
        )
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return tuple(data)
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return (
        "ui",
        "screenshot",
        "diagram",
        "chart",
        "pixel",
        "layout",
        "error",
        "diff",
        "схем",
        "скрин",
        "интерфейс",
    )


_VISION_TRIGGERS = _load_intent_markers()


def _strict(data: Mapping[str, Any], required: set[str], optional: set[str] = set()) -> None:
    if not isinstance(data, Mapping):
        raise ValueError("object required")
    missing = required - set(data)
    unknown = set(data) - required - optional
    if missing:
        raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")


def _schema(value: Any) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValueError("unsupported visual-intake schema version")
    return value


def _text(value: Any, name: str, limit: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{name} must be a string")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit}")
    return value


def _string_tuple(
    value: Any, name: str, *, nonempty: bool = False, limit: int = 50
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError(f"{name} must be a list of at most {limit}")
    result = tuple(_text(item, name, 2000, allow_empty=False) for item in value)
    if nonempty and not result:
        raise ValueError(f"{name} must not be empty")
    return result


@dataclass(frozen=True)
class VisualKeyElement:
    """Represent a notable visual element with optional location."""

    type: str
    text: str
    description: str
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "text": self.text,
            "description": self.description,
            "location": self.location,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualKeyElement":
        _strict(data, {"type", "text", "description", "location"})
        location = data["location"]
        if location is not None:
            location = _text(location, "location", 2000)
        return cls(
            _text(data["type"], "type", 2000, allow_empty=False),
            _text(data["text"], "text", 2000),
            _text(data["description"], "description", 2000),
            location,
        )


@dataclass(frozen=True)
class VisualObservation:
    """Represent a factual or inferred visual observation."""

    kind: str
    text: str
    attachment_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "attachment_ids": list(self.attachment_ids)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualObservation":
        _strict(data, {"kind", "text"}, {"attachment_ids"})
        kind = _text(data["kind"], "kind", 20, allow_empty=False)
        if kind not in _OBSERVATION_KINDS:
            raise ValueError("observation kind must be fact or inference")
        return cls(
            kind,
            _text(data["text"], "observation text", 2000, allow_empty=False),
            _string_tuple(data.get("attachment_ids", []), "attachment_ids"),
        )


@dataclass(frozen=True)
class VisualIntakeResultV1:
    """Represent a validated schema-v1 visual analysis result."""

    schema_version: int
    role_version: str
    attachment_ids: tuple[str, ...]
    image_type: str
    summary: str
    visible_text: str
    key_elements: tuple[VisualKeyElement, ...]
    observations: tuple[VisualObservation, ...]
    likely_relevance: tuple[str, ...]
    uncertainties: tuple[str, ...]
    needs_deeper_visual_analysis: bool
    confidence: float

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported visual-intake schema version")
        _text(self.role_version, "role_version", 100, allow_empty=False)
        if not self.attachment_ids or len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("attachment_ids must be non-empty and unique")
        if self.image_type not in IMAGE_TYPES:
            raise ValueError("invalid image_type")
        if not isinstance(self.needs_deeper_visual_analysis, bool):
            raise ValueError("needs_deeper_visual_analysis must be boolean")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be finite and within [0,1]")
        if (
            self.needs_deeper_visual_analysis
            or self.confidence < LOW_CONFIDENCE_THRESHOLD
            or self.image_type == "unknown"
        ) and not self.uncertainties:
            raise ValueError("uncertainties required for ambiguous results")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role_version": self.role_version,
            "attachment_ids": list(self.attachment_ids),
            "image_type": self.image_type,
            "summary": self.summary,
            "visible_text": self.visible_text,
            "key_elements": [x.to_dict() for x in self.key_elements],
            "observations": [x.to_dict() for x in self.observations],
            "likely_relevance": list(self.likely_relevance),
            "uncertainties": list(self.uncertainties),
            "needs_deeper_visual_analysis": self.needs_deeper_visual_analysis,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualIntakeResultV1":
        required = {
            "schema_version",
            "role_version",
            "attachment_ids",
            "image_type",
            "summary",
            "visible_text",
            "key_elements",
            "observations",
            "likely_relevance",
            "uncertainties",
            "needs_deeper_visual_analysis",
            "confidence",
        }
        _strict(data, required)
        for name in ("key_elements", "observations"):
            if not isinstance(data[name], list) or len(data[name]) > 50:
                raise ValueError(f"{name} must be a list of at most 50")
        return cls(
            _schema(data["schema_version"]),
            _text(data["role_version"], "role_version", 100, allow_empty=False),
            _string_tuple(data["attachment_ids"], "attachment_ids", nonempty=True),
            _text(data["image_type"], "image_type", 40, allow_empty=False),
            _text(data["summary"], "summary", 4000),
            _text(data["visible_text"], "visible_text", 20000),
            tuple(VisualKeyElement.from_dict(x) for x in data["key_elements"]),
            tuple(VisualObservation.from_dict(x) for x in data["observations"]),
            _string_tuple(data["likely_relevance"], "likely_relevance"),
            _string_tuple(data["uncertainties"], "uncertainties"),
            data["needs_deeper_visual_analysis"],
            data["confidence"],
        )


@dataclass(frozen=True)
class VisualIntakeErrorV1:
    """Represent a schema-v1 visual analysis error."""

    schema_version: int
    attachment_ids: tuple[str, ...]
    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.code not in _ERROR_CODES:
            raise ValueError("invalid visual-intake error")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be boolean")
        _text(self.message, "message", 1000, allow_empty=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attachment_ids": list(self.attachment_ids),
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualIntakeErrorV1":
        _strict(data, {"schema_version", "attachment_ids", "code", "message", "retryable"})
        return cls(
            _schema(data["schema_version"]),
            _string_tuple(data["attachment_ids"], "attachment_ids"),
            _text(data["code"], "code", 40),
            _text(data["message"], "message", 1000, allow_empty=False),
            data["retryable"],
        )


@dataclass(frozen=True)
class AttachmentRef:
    """Represent an attachment identity, content hash, and MIME type."""

    attachment_id: str
    content_hash: str
    mime: str

    def __post_init__(self) -> None:
        _text(self.attachment_id, "attachment_id", 500, allow_empty=False)
        if not _SHA256.fullmatch(self.content_hash):
            raise ValueError("content_hash must be lowercase sha256 hex")
        _text(self.mime, "mime", 200, allow_empty=False)

    def to_dict(self) -> dict[str, str]:
        return {
            "attachment_id": self.attachment_id,
            "content_hash": self.content_hash,
            "mime": self.mime,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AttachmentRef":
        _strict(data, {"attachment_id", "content_hash", "mime"})
        return cls(str(data["attachment_id"]), str(data["content_hash"]), str(data["mime"]))


@dataclass(frozen=True)
class VisualIntakePolicy:
    """Configure whether visual intake runs and at what detail."""

    mode: str = "auto"
    detail: str = "auto"
    schema_version: int = SCHEMA_VERSION
    role_version: str = ROLE_VERSION

    def __post_init__(self) -> None:
        if (
            self.mode not in {"off", "auto", "always"}
            or self.detail not in {"low", "auto", "high"}
            or self.schema_version != SCHEMA_VERSION
        ):
            raise ValueError("invalid visual-intake policy")
        _text(self.role_version, "role_version", 100, allow_empty=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "detail": self.detail,
            "schema_version": self.schema_version,
            "role_version": self.role_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualIntakePolicy":
        _strict(data, {"mode", "detail", "schema_version", "role_version"})
        return cls(
            str(data["mode"]),
            str(data["detail"]),
            _schema(data["schema_version"]),
            str(data["role_version"]),
        )


@dataclass(frozen=True)
class VisualIntakeRequestV1:
    """Represent a schema-v1 visual analysis request."""

    user_text: str
    attachments: tuple[AttachmentRef, ...]
    policy: VisualIntakePolicy
    focus: str | None = None
    detail: str | None = None
    compare: bool = False

    def __post_init__(self) -> None:
        _text(self.user_text, "user_text", 20000)
        if len(self.attachments) > 50 or len({a.attachment_id for a in self.attachments}) != len(
            self.attachments
        ):
            raise ValueError("attachments must have unique ids and at most 50 entries")
        if self.focus is not None:
            _text(self.focus, "focus", 2000)
        if self.detail is not None and self.detail not in {"low", "auto", "high"}:
            raise ValueError("invalid detail")
        if not isinstance(self.compare, bool):
            raise ValueError("compare must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_text": self.user_text,
            "attachments": [x.to_dict() for x in self.attachments],
            "policy": self.policy.to_dict(),
            "focus": self.focus,
            "detail": self.detail,
            "compare": self.compare,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualIntakeRequestV1":
        _strict(data, {"user_text", "attachments", "policy"}, {"focus", "detail", "compare"})
        if not isinstance(data["user_text"], str) or not isinstance(data["attachments"], list):
            raise ValueError("user_text must be a string and attachments must be a list")
        return cls(
            data["user_text"],
            tuple(AttachmentRef.from_dict(x) for x in data["attachments"]),
            VisualIntakePolicy.from_dict(data["policy"]),
            data.get("focus"),
            data.get("detail"),
            data.get("compare", False),
        )


@dataclass(frozen=True)
class VisualIntakeHandoff:
    """Carry visual context, result or error, telemetry, and retained IDs."""

    compact_context: str
    result: VisualIntakeResultV1 | None
    error: VisualIntakeErrorV1 | None
    telemetry: dict[str, Any]
    conductor_received_visual_context: bool
    retained_attachment_ids: tuple[str, ...]
    used_fallback: bool
    cached: bool
    role: str | None
    model: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "compact_context": self.compact_context,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error.to_dict() if self.error else None,
            "telemetry": dict(self.telemetry),
            "conductor_received_visual_context": self.conductor_received_visual_context,
            "retained_attachment_ids": list(self.retained_attachment_ids),
            "used_fallback": self.used_fallback,
            "cached": self.cached,
            "role": self.role,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualIntakeHandoff":
        required = {
            "compact_context",
            "result",
            "error",
            "telemetry",
            "conductor_received_visual_context",
            "retained_attachment_ids",
            "used_fallback",
            "cached",
            "role",
            "model",
        }
        _strict(data, required)
        if not isinstance(data["telemetry"], Mapping):
            raise ValueError("telemetry must be an object")
        result = (
            VisualIntakeResultV1.from_dict(data["result"])
            if isinstance(data["result"], Mapping)
            else None
        )
        error = (
            VisualIntakeErrorV1.from_dict(data["error"])
            if isinstance(data["error"], Mapping)
            else None
        )
        if (
            data["result"] is not None
            and result is None
            or data["error"] is not None
            and error is None
        ):
            raise ValueError("result and error must be objects or null")
        bools = (data["conductor_received_visual_context"], data["used_fallback"], data["cached"])
        if not all(isinstance(value, bool) for value in bools):
            raise ValueError("handoff flags must be boolean")
        for name in ("role", "model"):
            if data[name] is not None and not isinstance(data[name], str):
                raise ValueError(f"{name} must be a string or null")
        return cls(
            _text(data["compact_context"], "compact_context", 300000),
            result,
            error,
            dict(data["telemetry"]),
            bools[0],
            _string_tuple(data["retained_attachment_ids"], "retained_attachment_ids"),
            bools[1],
            bools[2],
            data["role"],
            data["model"],
        )


class VisualInvoker(Protocol):
    """Define the invoker contract for a visual-intake role."""

    def invoke(
        self,
        *,
        role: str,
        model: str,
        request: VisualIntakeRequestV1,
        repair_errors: tuple[str, ...] | None = None,
    ) -> Mapping[str, Any]: ...


def validate_visual_intake_result(data: Mapping[str, Any]) -> VisualIntakeResultV1:
    """Validate a visual result and cross-check observation attachment IDs."""
    result = VisualIntakeResultV1.from_dict(data)
    result_ids = set(result.attachment_ids)
    if any(
        not set(observation.attachment_ids).issubset(result_ids)
        for observation in result.observations
    ):
        raise ValueError("observation attachment_ids must be a subset of result attachment_ids")
    return result


def compact_visual_context(
    user_text: str,
    result_or_error: VisualIntakeResultV1 | VisualIntakeErrorV1,
    attachment_ids: tuple[str, ...] | list[str],
) -> str:
    """Render a bounded textual handoff from a visual result or error."""
    ids = tuple(str(x) for x in attachment_ids)
    lines = ["USER REQUEST", user_text or "(empty)", "", "VISUAL CONTEXT"]
    if isinstance(result_or_error, VisualIntakeErrorV1):
        lines += [
            "source: visual-intake (auxiliary observation, not ground truth)",
            f"Visual analysis unavailable: {result_or_error.code}: {result_or_error.message}",
        ]
    else:
        result = result_or_error
        lines += ["source: visual-intake (auxiliary observation, not ground truth)"]
        lines += [f"Attachment {item}: {result.image_type}" for item in ids]
        lines += [f"Summary: {result.summary}", f"Visible text: {result.visible_text}", "Facts:"]
        lines += [f"- {x.text}" for x in result.observations if x.kind == "fact"] or ["- (none)"]
        lines += ["Inferences:"] + (
            [f"- {x.text}" for x in result.observations if x.kind == "inference"] or ["- (none)"]
        )
        lines += ["Key elements:"] + (
            [
                f"- {x.type}: {x.text}; {x.description}"
                + (f" ({x.location})" if x.location else "")
                for x in result.key_elements
            ]
            or ["- (none)"]
        )
        lines += ["Likely relevance:"] + (
            [f"- {x}" for x in result.likely_relevance] or ["- (none)"]
        )
        lines += [
            f"Confidence: {result.confidence:.2f}",
            f"Deeper analysis suggested: {'yes' if result.needs_deeper_visual_analysis else 'no'}",
            "",
            "UNCERTAINTIES",
        ]
        lines += [f"- {x}" for x in result.uncertainties] or ["- (none)"]
    lines += ["", "ATTACHMENTS"] + list(ids)
    return "\n".join(lines)


def cache_safe_result(result: VisualIntakeResultV1) -> dict[str, Any]:
    # Cache hits intentionally retain operational metadata but never extracted text.
    """Return cache-safe visual metadata without extracted text."""
    return {
        "schema_version": result.schema_version,
        "role_version": result.role_version,
        "attachment_count": len(result.attachment_ids),
        "image_type": result.image_type,
        "summary": "",
        "visible_text": "",
        "key_elements": [],
        "observations": [],
        "likely_relevance": [],
        "uncertainties": [],
        "needs_deeper_visual_analysis": result.needs_deeper_visual_analysis,
        "confidence": result.confidence,
    }


def _cached_result(
    data: Mapping[str, Any], attachments: tuple[AttachmentRef, ...]
) -> VisualIntakeResultV1:
    required = {
        "schema_version",
        "role_version",
        "attachment_count",
        "attachment_hashes",
        "image_type",
        "summary",
        "visible_text",
        "key_elements",
        "observations",
        "likely_relevance",
        "uncertainties",
        "needs_deeper_visual_analysis",
        "confidence",
    }
    _strict(data, required)
    hashes = _string_tuple(data["attachment_hashes"], "attachment_hashes", nonempty=True)
    current_hashes = tuple(attachment.content_hash for attachment in attachments)
    if (
        type(data["attachment_count"]) is not int
        or data["attachment_count"] != len(attachments)
        or hashes != current_hashes
    ):
        raise ValueError("cached attachments do not match request")
    uncertainties: list[str] = []
    if (
        data["needs_deeper_visual_analysis"]
        or data["confidence"] < LOW_CONFIDENCE_THRESHOLD
        or data["image_type"] == "unknown"
    ):
        uncertainties = ["Cached visual details were not retained."]
    remapped = dict(data)
    remapped.pop("attachment_count")
    remapped.pop("attachment_hashes")
    remapped["attachment_ids"] = [attachment.attachment_id for attachment in attachments]
    remapped["uncertainties"] = uncertainties
    return validate_visual_intake_result(remapped)


def _compact_cached_context(
    user_text: str, result: VisualIntakeResultV1, attachment_ids: tuple[str, ...]
) -> str:
    lines = [
        "USER REQUEST",
        user_text or "(empty)",
        "",
        "VISUAL CONTEXT",
        "source: visual-intake (auxiliary observation, not ground truth)",
        "cached: true",
        "Visual analysis was previously computed; extracted text was not retained.",
        f"Image type: {result.image_type}",
        f"Confidence: {result.confidence:.2f}",
        f"Deeper analysis suggested: {'yes' if result.needs_deeper_visual_analysis else 'no'}",
        "",
        "ATTACHMENTS",
        *attachment_ids,
    ]
    return "\n".join(lines)


def _normalized_mime(mime: str) -> str:
    return mime.split(";", 1)[0].strip().casefold()


def visual_cache_key(
    hashes_in_order: list[str] | tuple[str, ...],
    schema_version: int,
    role_version: str,
    options_dict: Mapping[str, Any],
    binding_version: str,
) -> str:
    """Build a deterministic cache key from ordered hashes and binding options."""
    payload = {
        "hashes": list(hashes_in_order),
        "schema_version": schema_version,
        "role_version": role_version,
        "options": dict(options_dict),
        "binding_version": binding_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def binding_version(
    role_name: str,
    model_id: str,
    provider: str,
    effort: str | None,
    catalog_version: int | str,
    extra_options: Mapping[str, Any] | None = None,
) -> str:
    """Build a deterministic binding version while excluding sensitive options."""
    sensitive = ("key", "token", "secret", "credential", "authorization", "password", "bearer")
    options = {
        str(key): value
        for key, value in (extra_options or {}).items()
        if not any(marker in str(key).casefold() for marker in sensitive)
    }
    payload = {
        "role": role_name,
        "model": model_id,
        "provider": provider,
        "effort": effort,
        "catalog_version": catalog_version,
        "options": options,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _role_eligible(role: Any, registry: RoleRegistry, state: Any = None) -> bool:
    if role is None or not role.model or role.capabilities.vision is not True:
        return False
    meta = registry.provider_catalog.get(role.model)
    if not credential_present(meta, os.environ):
        return False
    if state is not None and (
        (hasattr(state, "is_available") and not state.is_available(role.name))
        or (hasattr(state, "circuit_open") and state.circuit_open(role.name))
    ):
        return False
    return True


def resolve_visual_role(
    registry: RoleRegistry, state: Any = None, prefer_deep: bool = False
) -> tuple[Any, str] | None:
    """Resolve an eligible visual-intake role and model."""
    names = ("visual-intake-deep",) if prefer_deep else ("visual-intake",)
    for name in names:
        role = registry.get(name)
        if _role_eligible(role, registry, state):
            return role, role.model
    return None


def should_use_deep_intake(
    request: VisualIntakeRequestV1, result: VisualIntakeResultV1 | None = None
) -> bool:
    """Determine whether a request or result warrants deeper visual analysis."""
    text = request.user_text.casefold().strip()
    keywords = _VISION_TRIGGERS
    return bool(
        (len(request.attachments) > 1 and request.compare)
        or len(text) < 12
        or (request.detail or request.policy.detail) == "high"
        or any(k in text for k in keywords)
        or (
            result
            and (
                result.confidence < LOW_CONFIDENCE_THRESHOLD or result.needs_deeper_visual_analysis
            )
        )
    )


def select_mode_action(
    policy: VisualIntakePolicy, has_supported_images: bool, has_any_attachments: bool
) -> str:
    """Select the visual-intake action for policy and attachment support."""
    if policy.mode == "off":
        return "skip"
    if has_supported_images:
        return "invoke"
    if has_any_attachments:
        return "unsupported"
    return "no_attachment_marker" if policy.mode == "always" else "skip"


def _strip_binary(value: Any, ids: list[str]) -> Any:
    if isinstance(value, Mapping):
        kind = str(value.get("type", "")).casefold()
        if kind in {"image", "image_url", "input_image", "binary", "file"} or any(
            k in value for k in ("data", "bytes", "image_url", "base64")
        ):
            attachment_id = value.get("attachment_id") or value.get("id")
            if attachment_id is not None:
                ids.append(str(attachment_id))
            return None
        return {
            str(k): cleaned
            for k, v in value.items()
            if (cleaned := _strip_binary(v, ids)) is not None
        }
    if isinstance(value, list):
        return [cleaned for v in value if (cleaned := _strip_binary(v, ids)) is not None]
    return value


def conductor_provider_payload(
    canonical_turn: Mapping[str, Any], vision_capable: bool
) -> dict[str, Any]:
    """Strip binary visual content when the conductor lacks vision."""
    payload = dict(canonical_turn)
    if vision_capable:
        return payload
    ids = (
        [str(x) for x in payload.get("canonical_attachments", [])]
        if isinstance(payload.get("canonical_attachments"), list)
        else []
    )
    cleaned = _strip_binary(payload, ids)
    cleaned["canonical_attachments"] = list(dict.fromkeys(ids))
    return cleaned


def load_visual_intake_policy(config_path: Path | str) -> VisualIntakePolicy:
    """Load visual-intake policy from configuration with safe defaults."""
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
        spec = raw.get("visual_intake", {})
        if not isinstance(spec, Mapping):
            spec = {}
        return VisualIntakePolicy(
            str(spec.get("mode", "auto")),
            str(spec.get("detail", "auto")),
            int(spec.get("schema_version", SCHEMA_VERSION)),
            str(spec.get("role_version", ROLE_VERSION)),
        )
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return VisualIntakePolicy()


def _error(code: str, ids: tuple[str, ...], message: str, retryable: bool) -> VisualIntakeErrorV1:
    return VisualIntakeErrorV1(SCHEMA_VERSION, ids, code, message, retryable)


def run_visual_intake(
    request: VisualIntakeRequestV1,
    *,
    registry: RoleRegistry,
    invoker: VisualInvoker,
    state: Any = None,
    cache_path: Path | str | None = None,
    catalog_version: int | str = 2,
    prefer_deep: bool | None = None,
) -> VisualIntakeHandoff:
    """Run visual analysis with caching, fallback roles, validation, and telemetry."""
    ids = tuple(a.attachment_id for a in request.attachments)
    supported = tuple(
        a for a in request.attachments if _normalized_mime(a.mime) in SUPPORTED_IMAGE_MIMES
    )
    supported_ids = tuple(a.attachment_id for a in supported)
    action = select_mode_action(request.policy, bool(supported), bool(request.attachments))
    telemetry: dict[str, Any] = {
        "visual_intake_invoked": False,
        "visual_intake_cached": False,
        "visual_intake_retry_count": 0,
        "visual_intake_fallback_used": False,
        "conductor_received_visual_context": False,
    }

    def handoff(
        result: VisualIntakeResultV1 | None = None,
        error: VisualIntakeErrorV1 | None = None,
        *,
        received: bool,
        cached: bool = False,
        fallback: bool = False,
        role: Any = None,
    ) -> VisualIntakeHandoff:
        context_ids = supported_ids if result else ids
        if cached and result:
            context = _compact_cached_context(request.user_text, result, context_ids)
        else:
            context = (
                compact_visual_context(request.user_text, result or error, context_ids)
                if (result or error)
                else ""
            )
        telemetry["conductor_received_visual_context"] = received
        telemetry["visual_intake_cached"] = cached
        telemetry["visual_intake_fallback_used"] = fallback
        if result:
            telemetry["visual_intake_confidence"] = result.confidence
        return VisualIntakeHandoff(
            context,
            result,
            error,
            telemetry,
            received,
            ids,
            fallback,
            cached,
            role.name if role else None,
            role.model if role else None,
        )

    if action == "skip":
        return handoff(received=False)
    if action == "no_attachment_marker":
        return handoff(
            error=_error("unsupported_media", ids, "No image attachment was provided.", False),
            received=True,
        )
    if action == "unsupported":
        error = _error(
            "unsupported_media", ids, "No supported image attachment was provided.", False
        )
        return handoff(error=error, received=True)

    invocation_request = VisualIntakeRequestV1(
        request.user_text, supported, request.policy, request.focus, request.detail, request.compare
    )
    deep = should_use_deep_intake(invocation_request) if prefer_deep is None else prefer_deep
    resolved = resolve_visual_role(registry, state, deep)
    if resolved is None and deep:
        resolved = resolve_visual_role(registry, state, False)
    if resolved is None:
        return handoff(
            error=_error(
                "binding_unavailable", ids, "No eligible visual-intake binding is available.", True
            ),
            received=True,
        )
    role, _model = resolved
    options = {
        "detail": request.detail or request.policy.detail,
        "focus": request.focus,
        "compare": request.compare,
    }

    def role_cache_key(current: Any) -> str:
        return visual_cache_key(
            [a.content_hash for a in supported],
            request.policy.schema_version,
            request.policy.role_version,
            options,
            binding_version(
                current.name,
                current.model,
                current.provider,
                current.reasoning_effort,
                catalog_version,
            ),
        )

    cached_data = visual_cache_get(role_cache_key(role), cache_path)
    if cached_data and cached_data.get("kind") == "result":
        try:
            return handoff(
                result=_cached_result(cached_data["value"], supported),
                received=True,
                cached=True,
                role=role,
            )
        except (KeyError, TypeError, ValueError):
            pass

    started = time.monotonic()
    last_invalid = False
    fallback_attempted = False

    def invoke_role(current: Any, *, fallback: bool) -> VisualIntakeResultV1 | None:
        nonlocal last_invalid, fallback_attempted
        if not _role_eligible(current, registry, state):
            return None
        if fallback:
            fallback_attempted = True
        telemetry["visual_intake_invoked"] = True
        telemetry["visual_intake_binding"] = binding_version(
            current.name, current.model, current.provider, current.reasoning_effort, catalog_version
        )
        for repair in range(2):
            try:
                raw = invoker.invoke(
                    role=current.name,
                    model=current.model,
                    request=invocation_request,
                    repair_errors=("invalid schema",) if repair else None,
                )
                result = validate_visual_intake_result(raw)
                if result.attachment_ids != supported_ids:
                    raise ValueError("attachment ids do not match request")
                telemetry["visual_intake_retry_count"] += repair
                telemetry["visual_intake_latency"] = max(0.0, time.monotonic() - started)
                for field in ("input_tokens", "output_tokens"):
                    value = raw.get(f"_{field}") if isinstance(raw, Mapping) else None
                    if isinstance(value, int) and value >= 0:
                        telemetry[f"visual_intake_{field}"] = value
                safe = cache_safe_result(result)
                safe["attachment_hashes"] = [a.content_hash for a in supported]
                visual_cache_put(
                    role_cache_key(current), {"kind": "result", "value": safe}, cache_path
                )
                last_invalid = False
                return result
            except (KeyError, TypeError, ValueError):
                last_invalid = True
                if repair == 0:
                    continue
            except Exception:  # invocation boundary
                last_invalid = False
                break
        return None

    chain = [role.name, *role.fallback]
    attempted_names: set[str] = set()
    for index, role_name in enumerate(chain):
        current = registry.get(role_name)
        if (
            current is None
            or current.name in attempted_names
            or not _role_eligible(current, registry, state)
        ):
            continue
        attempted_names.add(current.name)
        result = invoke_role(current, fallback=index > 0)
        if result is None:
            continue
        if current.name != "visual-intake-deep" and should_use_deep_intake(
            invocation_request, result
        ):
            deep_names = ["visual-intake-deep", *current.fallback]
            for deep_name in deep_names:
                deeper_role = registry.get(deep_name)
                if (
                    deeper_role is None
                    or deeper_role.name in attempted_names
                    or not _role_eligible(deeper_role, registry, state)
                ):
                    continue
                attempted_names.add(deeper_role.name)
                deep_result = invoke_role(deeper_role, fallback=True)
                if deep_result is not None:
                    return handoff(
                        result=deep_result, received=True, fallback=True, role=deeper_role
                    )
            return handoff(result=result, received=True, fallback=fallback_attempted, role=current)
        return handoff(result=result, received=True, fallback=index > 0, role=current)

    telemetry["visual_intake_latency"] = max(0.0, time.monotonic() - started)
    code = "invalid_output" if last_invalid else "analysis_failed"
    return handoff(
        error=_error(code, ids, "Visual analysis did not produce a valid result.", True),
        received=True,
        fallback=fallback_attempted,
        role=role,
    )


def request_visual_analysis(
    attachment_ids: tuple[str, ...] | list[str],
    focus: str | None,
    detail: str,
    *,
    attachments: Mapping[str, AttachmentRef],
    registry: RoleRegistry,
    invoker: VisualInvoker,
    policy: VisualIntakePolicy | None = None,
    state: Any = None,
    cache_path: Path | str | None = None,
) -> VisualIntakeHandoff:
    """Request visual analysis for known attachment IDs."""
    ids = tuple(str(x) for x in attachment_ids)
    known = tuple(attachments[x] for x in ids if x in attachments)
    if not ids or len(known) != len(ids):
        error = _error(
            "unsupported_media", ids, "Attachment ids must be non-empty and known.", False
        )
        telemetry = {"visual_intake_invoked": False, "conductor_received_visual_context": True}
        return VisualIntakeHandoff(
            compact_visual_context("", error, ids),
            None,
            error,
            telemetry,
            True,
            ids,
            False,
            False,
            None,
            None,
        )
    chosen = policy or VisualIntakePolicy(mode="always", detail=detail)
    if chosen.mode == "off":
        error = _error("policy_disabled", ids, "Visual analysis is disabled by policy.", False)
        telemetry = {"visual_intake_invoked": False, "conductor_received_visual_context": True}
        return VisualIntakeHandoff(
            compact_visual_context("", error, ids),
            None,
            error,
            telemetry,
            True,
            ids,
            False,
            False,
            None,
            None,
        )
    request = VisualIntakeRequestV1("", known, chosen, focus, detail, len(ids) > 1)
    return run_visual_intake(
        request,
        registry=registry,
        invoker=invoker,
        state=state,
        cache_path=cache_path,
        prefer_deep=True,
    )


__all__ = [
    name
    for name in globals()
    if name.startswith("Visual")
    or name
    in {
        "AttachmentRef",
        "IMAGE_TYPES",
        "SUPPORTED_IMAGE_MIMES",
        "SCHEMA_VERSION",
        "ROLE_VERSION",
        "LOW_CONFIDENCE_THRESHOLD",
        "validate_visual_intake_result",
        "compact_visual_context",
        "cache_safe_result",
        "visual_cache_key",
        "binding_version",
        "resolve_visual_role",
        "should_use_deep_intake",
        "select_mode_action",
        "conductor_provider_payload",
        "load_visual_intake_policy",
        "run_visual_intake",
        "request_visual_analysis",
    }
]
