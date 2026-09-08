from __future__ import annotations

from grokbuild.evidence import (
    FailureSignal,
    classify_failure,
    is_invalid_reasoning_effort_failure,
    retry_without_reasoning_effort,
)
from grokbuild.roles import load_provider_catalog, load_registry, sanitize_reasoning_effort


def test_security_max_clamps_to_grok_xhigh() -> None:
    assert sanitize_reasoning_effort("grok-4.6", "max") == "xhigh"
    assert sanitize_reasoning_effort("grok-4.6", "xhigh") == "xhigh"


def test_cataloged_role_bindings_have_supported_efforts() -> None:
    registry = load_registry()
    catalog = load_provider_catalog()
    for role in registry._roles.values():
        if role.model is None or role.reasoning_effort is None:
            continue
        meta = catalog.get(role.model)
        if meta is not None and meta.supported_reasoning_efforts is not None:
            assert role.reasoning_effort in meta.supported_reasoning_efforts, role.name


def test_invalid_effort_gets_one_default_effort_retry() -> None:
    signal = FailureSignal(
        status=400,
        reason="invalid-argument: Invalid reasoning effort",
        text="API error",
    )
    assert is_invalid_reasoning_effort_failure(signal)
    assert classify_failure(signal) == "model"
    assert retry_without_reasoning_effort(signal)
    assert not retry_without_reasoning_effort(signal, already_retried=True)


def test_second_invalid_effort_is_typed_model_failure() -> None:
    signal = FailureSignal(status=400, text="invalid-argument: reasoning_effort unsupported")
    assert classify_failure(signal) == "model"
    assert not retry_without_reasoning_effort(signal, already_retried=True)
