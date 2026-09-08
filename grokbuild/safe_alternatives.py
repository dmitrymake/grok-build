"""Reviewed safe-alternative hints for bounded denial classes."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

_REQUIRED_FIELDS = ("kind", "description", "reversibility", "operator_context")
_REVERSIBILITY = frozenset({"reversible", "unknown"})

SAFE_ALTERNATIVES: dict[str, dict[str, str]] = {
    "deletion": {
        "kind": "rename",
        "description": "Preserve the data by renaming or moving it before considering deletion.",
        "reversibility": "reversible",
        "operator_context": "Confirm the destination, available capacity, and a rollback path.",
    },
    "server_configuration": {
        "kind": "query_setting",
        "description": "Prefer a scoped query-level setting over changing server configuration.",
        "reversibility": "reversible",
        "operator_context": "Record the affected query and verify that the setting expires with its scope.",
    },
    "manual_application": {
        "kind": "recovery_workflow",
        "description": "Use the supported recovery workflow instead of applying state manually.",
        "reversibility": "unknown",
        "operator_context": "Confirm workflow prerequisites and retain recovery evidence for operator review.",
    },
    "shell_not_readonly": {
        "kind": "delegate_writable_child",
        "description": "Delegate the command to a writable child because it could not be proven read-only.",
        "reversibility": "reversible",
        "operator_context": "Use the next required stage and keep the conductor read-only.",
    },
    "zero_write": {
        "kind": "delegate_writable_child",
        "description": "Delegate the change to the required writable implementation child.",
        "reversibility": "reversible",
        "operator_context": "Use the next required stage and keep the conductor read-only.",
    },
    "recon_diet": {
        "kind": "delegated_recon",
        "description": "Run the required delegated reconnaissance before mapping the repository.",
        "reversibility": "reversible",
        "operator_context": "Spawn explore or explore-thorough according to the required stage recipe.",
    },
    "pending_verify": {
        "kind": "complete_verification",
        "description": "Run the exact configured deterministic verifier or spawn the required review stage.",
        "reversibility": "reversible",
        "operator_context": "Follow the pending stage recipe without substituting an unconfigured verifier.",
    },
    "wrong_stage": {
        "kind": "required_stage",
        "description": "Run the next required stage rather than the requested out-of-order stage.",
        "reversibility": "reversible",
        "operator_context": "Use the role and stage identified by the denial recipe.",
    },
    "wrong_parallel_member": {
        "kind": "required_parallel_member",
        "description": "Spawn an outstanding member of the current parallel barrier.",
        "reversibility": "reversible",
        "operator_context": "Use an unclaimed member from the barrier recipe and preserve its barrier key.",
    },
    "routing_config_unavailable": {
        "kind": "read_only_fallback",
        "description": "Remain read-only until routing configuration is available again.",
        "reversibility": "reversible",
        "operator_context": "Inspect configuration through read tools; do not infer write authorization.",
    },
}

_OPERATION_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "deletion",
        (re.compile(r"(?:^|[;&|]\s*|\bsudo\s+)(?:rm|rmdir|unlink)\b", re.IGNORECASE),),
    ),
    (
        "server_configuration",
        (
            re.compile(r"\b(?:systemctl\s+edit|sysctl\s+-w)\b", re.IGNORECASE),
            re.compile(
                r"\b(?:sed\s+-i|tee|cp|mv)\b[^\n]*(?:/etc/|server[^/\s]*\.conf\b)",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "manual_application",
        (re.compile(r"\b(?:kubectl|terraform|tofu)\s+apply\b", re.IGNORECASE),),
    ),
)


def classify_operation(command: str) -> str | None:
    """Classify only reviewed destructive terminal-operation shapes."""
    bounded = command[:4096]
    for operation_class, patterns in _OPERATION_PATTERNS:
        if any(pattern.search(bounded) for pattern in patterns):
            return operation_class
    return None


def safe_alternative(
    denial_kind: str,
    *,
    operation_class: str | None = None,
    mapping: Mapping[str, Any] | None = None,
) -> dict[str, str] | None:
    """Return a validated copy of a reviewed hint, or ``None`` when unusable."""
    table = SAFE_ALTERNATIVES if mapping is None else mapping
    key = operation_class if operation_class in table else denial_kind
    raw = table.get(key)
    if not isinstance(raw, Mapping):
        return None
    if set(raw) != set(_REQUIRED_FIELDS):
        return None
    hint = {field: raw.get(field) for field in _REQUIRED_FIELDS}
    if not all(isinstance(value, str) and value.strip() for value in hint.values()):
        return None
    if hint["reversibility"] not in _REVERSIBILITY:
        return None
    return hint


__all__ = ["SAFE_ALTERNATIVES", "classify_operation", "safe_alternative"]
