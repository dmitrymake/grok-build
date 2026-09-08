#!/usr/bin/env python3
"""Small, conservative pattern-based redaction helpers for outcome logs."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

_REPLACEMENT = "[redacted]"

_SENSITIVE_KEYS = frozenset(
    {
        "secret",
        "secrets",
        "token",
        "tokens",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "credential",
        "credentials",
        "private_key",
        "privatekey",
        "access_key",
        "accesskey",
    }
)

# Keep the fold deliberately small: these are the common Cyrillic/Latin lookalikes
# seen in credential field names and prefixes, not a general transliterator.
_CONFUSABLES = str.maketrans(
    {
        "\u0430": "a",
        "\u0410": "A",
        "\u0435": "e",
        "\u0415": "E",
        "\u043e": "o",
        "\u041e": "O",
        "\u0440": "p",
        "\u0420": "P",
        "\u0441": "c",
        "\u0421": "C",
        "\u0445": "x",
        "\u0425": "X",
        "\u0443": "y",
        "\u0423": "Y",
        "\u043a": "k",
        "\u041a": "K",
        "\u043c": "m",
        "\u041c": "M",
        "\u0442": "t",
        "\u0422": "T",
        "\u0432": "B",
        "\u0412": "B",
    }
)

# Patterns operate on folded text; all substitutions use the corresponding span
# in the original text, so the value's original spelling is retained only outside
# the redacted span.
_BEARER = re.compile(r"(?i)(bearer\s+)([^\s]+(?:\s*\n\s*[^\s]+)*)")
_ASSIGNMENT = re.compile(
    r"(?is)([\w-]*(?:token|secret|password|passwd|credential|api[_-]?key|access[_-]?key)\b\\?(?:[\"'])?\s*[=:]\s*)(\\?[\"'])(.*?)(\2)|"
    r"([\w-]*(?:token|secret|password|passwd|credential|api[_-]?key|access[_-]?key)\b\\?(?:[\"'])?\s*[=:]\s*)([^\s}\"'\n]+(?:\n\s*[^\s}\"'\n]+)?)"
)
_DSN = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^@\s]+)@")
_QUERY = re.compile(
    r"(?i)([?&](?:private[_-]?token|access[_-]?token|token|sig|x-amz-signature|api[_-]?key|secret|password|credential)\s*=\s*)([^&#\s]+)"
)
_PEM = re.compile(
    r"(?is)-{5}\s*BEGIN\s+(?:[A-Z0-9]+\s+)*PRIVATE\s+KEY\s*-{5}(?:.*?-{5}\s*END\s+(?:[A-Z0-9]+\s+)*PRIVATE\s+KEY\s*-{5}|.*\Z)"
)
_BARE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"(?:AKIA|ASIA)[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]*){0,2}|"
    r"AIza[0-9A-Za-z_-]{35}|[A-Za-z0-9+/]{40}|sk_(?:live|test)_[A-Za-z0-9_-]{8,}|"
    r"xapp-[A-Za-z0-9-]{8,}|glpat-[A-Za-z0-9_-]{8,}|SG\.[A-Za-z0-9_-]{10,}|"
    r"hf_[A-Za-z0-9_-]{8,}|npm_[A-Za-z0-9_-]{8,}|AGE-SECRET-KEY-1[A-Za-z0-9_-]{8,}|"
    r"(?:sk-|pk-|rk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_./-]{8,}"
    r")(?![A-Za-z0-9_-])"
)


def fold_confusables(text: str) -> str:
    """Fold common Cyrillic lookalikes into their Latin equivalents."""
    return text.translate(_CONFUSABLES)


def _replace(text: str, pattern: re.Pattern[str], replacement) -> str:
    folded = fold_confusables(text)
    matches = list(pattern.finditer(folded))
    for match in reversed(matches):
        original = text[match.start() : match.end()]
        text = text[: match.start()] + replacement(original, match) + text[match.end() :]
    return text


def _assignment_replacement(original: str, match: re.Match[str]) -> str:
    return _REPLACEMENT


def redact_text(text: str) -> str:
    if not text:
        return text
    text = _replace(text, _PEM, lambda _value, _match: _REPLACEMENT)
    text = _replace(
        text, _BEARER, lambda value, match: value[: match.start(2) - match.start()] + _REPLACEMENT
    )
    text = _replace(text, _ASSIGNMENT, _assignment_replacement)
    text = _replace(
        text,
        _DSN,
        lambda value, match: value[: match.start(2) - match.start()] + _REPLACEMENT + "@",
    )
    text = _replace(
        text, _QUERY, lambda value, match: value[: match.start(2) - match.start()] + _REPLACEMENT
    )
    return _replace(text, _BARE, lambda _value, _match: _REPLACEMENT)


_NORMALIZED_SENSITIVE_KEYS = frozenset(
    re.sub(r"[_-]", "", key.casefold()) for key in _SENSITIVE_KEYS
)


def redact(value: Any, max_len: int = 2000) -> Any:
    """Recursively redact strings, truncate long values, and drop secret keys."""
    if isinstance(value, str):
        return redact_text(value)[:max_len]
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[_-]", "", fold_confusables(str(key)).casefold())
            if any(sensitive in normalized for sensitive in _NORMALIZED_SENSITIVE_KEYS):
                out[str(key)] = _REPLACEMENT
            else:
                out[str(key)] = redact(item, max_len)
        return out
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact(item, max_len) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))[:max_len]


__all__ = ["redact", "redact_text"]
