#!/usr/bin/env python3
"""Check that fixture-free standalone tests are registered by their module."""

from __future__ import annotations

import ast
from pathlib import Path

from _harness import run_standalone


def test_standalone_registration() -> None:
    root = Path(__file__).parent
    violations: list[str] = []
    skipped: list[str] = []
    for path in sorted(root.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        if not any(isinstance(node, ast.If) and _is_main_guard(node) for node in tree.body):
            skipped.append(path.name)
            continue
        pytest_only = _pytest_only_names(tree)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            params = {arg.arg for arg in (*node.args.posonlyargs, *node.args.args)}
            if node.name in pytest_only:
                continue
            if params <= {"tmp"} and source.count(node.name) < 2:
                violations.append(f"{path.name}:{node.name}")
    print("pytest-only: " + ", ".join(skipped))
    assert not violations, "unregistered standalone tests: " + ", ".join(violations)


def _pytest_only_names(tree: ast.Module) -> set[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "PYTEST_ONLY" for target in node.targets):
                if isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
                    return {item.value for item in node.value.elts if isinstance(item, ast.Constant)}
    return set()


def _is_main_guard(node: ast.If) -> bool:
    return (
        isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


def main() -> int:
    test_standalone_registration()
    return 0


if __name__ == "__main__":
    run_standalone(main)
