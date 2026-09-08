#!/usr/bin/env python3
"""Fixture tests for the Grok intent router. No pytest required."""

from __future__ import annotations

from _harness import run_standalone

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

from grokbuild.classify import load_intents, match_needles
from grokbuild.corpus_sync import classify_provenance
from grokbuild.features import extract_features
from grokbuild.router import route_prompt

CASES: list[tuple[str, str | None]] = [
    ("прошей роутер на x86", "implement"),
    ("прошить firmware и поднять vpn", "implement"),
    ("flash the router with sysupgrade", "implement"),
    ("почини тесты в ai-console", "implement"),
    ("fix the build, tests are red", "implement"),
    ("напиши патч для demo-api", "implement"),
    ("рефактор sync.py под новые адаптеры", "implement"),
    ("найди RCE в demo-api", "security"),
    ("CVE-2026-1234 в vpn", "security"),
    ("сделай threat model прошивки", "security"),
    ("проверь прошивку на уязвимости", "security"),
    ("pentest этого firmware образа", "security"),
    ("прошей роутер и найди CVE", "security"),
    ("прошей роутер и сделай hardening", "implement"),
    ("спроектируй архитектуру ai-console v0.2", "plan"),
    ("write an ADR for the usage adapters", "plan"),
    ("заревьюить этот патч", "review"),
    ("отревьюить изменения", "review"),
    ("проревьюй код", "review"),
    ("review", "review"),
    ("code review please", "review"),
    ("проверь дифф", "review"),
    ("review the diff", "review"),
    ("найди RCE и review the diff", "security"),
    ("repo-wide migration and review the diff", "implement"),
    ("почему тесты красные, просто посмотри лог", None),
    ("что думаешь про terra как дефолт", None),
    ("summarize this file", None),
    ("как работает vpn, объясни", None),
    ("добавь пункт в README", "implement"),
    ("remove the deprecated option", "implement"),
    ("переименуй настройку", "implement"),
    ("update the label", "implement"),
    ("Добавь unit-тест для существующей функции", "implement"),
    ("Add a unit test for the existing function", "implement"),
    ("Сделай repo-wide refactor публичного API с миграцией", "implement"),
    ("migrate the database schema", "implement"),
    ("добавь контроль доступа в админку", "security"),
    ("implement access control for the admin API", "security"),
    ("исследуй структуру репозитория, только чтение без изменений", "explore"),
    ("explore the codebase read-only, no changes", "explore"),
    ("изучи структуру проекта без изменений", "explore"),
    ("research the repository structure", "explore"),
    ("deep research on database indexing", "research"),
    ("research approaches to caching", "research"),
    ("investigate approaches for API versioning", "research"),
    ("state of the art in vector search", "research"),
    ("landscape analysis of observability tools", "research"),
    ("compare approaches to rate limiting", "research"),
    ("research how browsers isolate processes", "research"),
    ("study approaches for backup recovery", "research"),
    ("market research for developer tools", "research"),
    ("research the ecosystem of package managers", "research"),
    ("глубокое исследование индексирования", "research"),
    ("исследуй подходы к кешированию", "research"),
    ("исследуй экосистему наблюдаемости", "research"),
    ("проанализируй подходы к версиям API", "research"),
    ("сравни подходы к ограничению запросов", "research"),
    ("исследование рынка IDE", "research"),
    ("изучи экосистему пакетов", "research"),
    ("изучи подходы к резервному копированию", "research"),
    (
        "<user_info>cwd has vpn and firmware</user_info>\n<user_query>\nsummarize this file\n</user_query>",
        None,
    ),
    ("<user_query>\nнайди RCE в demo-api\n</user_query>", "security"),
    ("summarize this file <system-reminder>найди RCE в demo-api</system-reminder>", "security"),
    ("<user_info>проверь прошивку на уязвимости</user_info>\nдобавь пункт в README", "security"),
    ("добавь пункт в README <system-reminder>прошей роутер</system-reminder>", "implement"),
    ("<system-reminder>найди RCE в demo-api</system-reminder>", "security"),
    ("summarize this file <system-reminder>review the diff</system-reminder>", "review"),
]
CLASSIFY_FAILED = 0


def test_classify_corpus() -> None:
    global CLASSIFY_FAILED
    failed = 0
    for prompt, expected in CASES:
        got = route_prompt(prompt, spec=load_intents(), mode="static").intent
        mark = "ok" if got == expected else "FAIL"
        if got != expected:
            failed += 1
        print(f"{mark:4} expected={expected!s:10} got={got!s:10}  {prompt}")
    CLASSIFY_FAILED = failed
    if failed:
        raise AssertionError(f"{failed} classification failures")


def test_classify_review_metadata() -> None:
    global CLASSIFY_FAILED
    review = route_prompt("review the diff", spec=load_intents(), mode="static")
    # RouteDecision.has_strong and block_tools are the canonical metadata equivalents.
    metadata_ok = review.has_strong and review.block_tools == ("search_replace", "write")
    print(f"{'ok' if metadata_ok else 'FAIL':4} review strong/edit-gated metadata")
    if not metadata_ok:
        CLASSIFY_FAILED += 1
        raise AssertionError("review strong/edit-gated metadata")


def test_english_marker_additions() -> None:
    spec = load_intents()
    markers = spec["markers"]
    destructive = [
        "reflash the device firmware",
        "flash firmware on the router",
        "flash the router firmware",
        "perform a firmware flash",
    ]
    high_complexity = ["thread safety", "thread-safety", "parallelism"]
    vision = ["interface", "user interface"]

    for prompt in destructive:
        features = extract_features(prompt, spec)
        assert match_needles(features.normalized, markers["destructive"])
        assert route_prompt(prompt, spec=spec, mode="static").intent == "implement"
    for prompt in high_complexity:
        features = extract_features(f"add {prompt} support in the service", spec)
        assert match_needles(features.normalized, markers["high_complexity"])
        decision = route_prompt(features.text, spec=spec, mode="static")
        assert decision.intent == "implement"
        assert decision.complexity == "high"
    for prompt in vision:
        request = f"update the {prompt} in the application"
        features = extract_features(request, spec)
        assert match_needles(features.normalized, markers["vision_triggers"])
        assert route_prompt(request, spec=spec, mode="static").intent == "implement"
    automation_prompt = "Work in the repository: fix the build"
    assert "Work in the repository:" in markers["automation_prefixes"]
    assert classify_provenance(automation_prompt)[1] == "automation-syntax"
    assert route_prompt(automation_prompt, spec=spec, mode="static").intent == "implement"

    negatives = ["flash sale pricing", "camera flash"]
    assert all(
        route_prompt(prompt, spec=spec, mode="static").intent is None for prompt in negatives
    )
    print("ok   English marker additions and negative controls")
    print("ok   English complexity and vision markers")
    print("ok   English automation prefix")


def main() -> int:
    global CLASSIFY_FAILED
    CLASSIFY_FAILED = 0
    for test in (
        test_classify_corpus,
        test_classify_review_metadata,
        test_english_marker_additions,
    ):
        try:
            test()
        except AssertionError:
            if test is test_english_marker_additions:
                CLASSIFY_FAILED += 1
    total = len(CASES) + 1
    print(f"{total - CLASSIFY_FAILED}/{total} passed")
    return 1 if CLASSIFY_FAILED else 0


if __name__ == "__main__":
    run_standalone(main)
