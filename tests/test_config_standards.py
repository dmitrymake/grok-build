from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON_WORDS = {"model", "provider", "family", "endpoint"}
DATE = re.compile(r"\b20\d\d\b|\b\d{4}-\d{2}-\d{2}\b")


def _catalog_terms() -> set[str]:
    catalog = json.loads((ROOT / "grokbuild" / "providers.json").read_text(encoding="utf-8"))
    providers = catalog.get("providers", {})
    terms: set[str] = set()
    for provider, spec in providers.items():
        terms.add(str(provider))
        if isinstance(spec, dict):
            terms.add(str(spec.get("label", "")))
            models = spec.get("models", {})
            if isinstance(models, dict):
                terms.update(str(model) for model in models)
    return {term for term in terms if term and term.casefold() not in COMMON_WORDS}


def _forbidden_patterns() -> list[re.Pattern[str]]:
    terms = set(_catalog_terms())
    for filename in ("config.toml", "config.example.toml"):
        config = tomllib.loads((ROOT / "config" / filename).read_text(encoding="utf-8"))
        terms.update(str(key) for key in config.get("model", {}))
    return [re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE) for term in terms]


def test_config_roles_have_effort_and_vendor_free_descriptions() -> None:
    patterns = _forbidden_patterns()
    for filename in ("config.toml", "config.example.toml"):
        config = tomllib.loads((ROOT / "config" / filename).read_text(encoding="utf-8"))
        roles = config["subagents"]["roles"]
        for name, role in roles.items():
            model = str(role.get("model", ""))
            if not model.startswith("grok-") and name not in {
                "visual-intake",
                "visual-intake-deep",
            }:
                assert isinstance(role.get("reasoning_effort"), str)
                assert role["reasoning_effort"].strip()
            description = str(role.get("description", ""))
            assert not DATE.search(description), f"{filename} {name} contains a date"
            for pattern in patterns:
                assert not pattern.search(description), (
                    f"{filename} {name} contains catalog term {pattern.pattern!r}"
                )
