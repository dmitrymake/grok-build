from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_grokbuild_module_imports_in_fresh_interpreter() -> None:
    modules = sorted(path.stem for path in (ROOT / "grokbuild").glob("*.py"))
    for module in modules:
        result = subprocess.run(
            [sys.executable, "-c", f"import grokbuild.{module}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"grokbuild.{module}: {result.stderr}"


def test_state_has_no_transaction_or_pipeline_imports() -> None:
    source = (ROOT / "grokbuild" / "state.py").read_text(encoding="utf-8")
    for forbidden in (
        "import pipeline",
        "from grokbuild.pipeline",
        "from grokbuild.transactions",
        "import transactions",
    ):
        assert forbidden not in source


def _top_level_imports(body: list[ast.stmt], edges: set[str]) -> None:
    """Collect grokbuild modules imported at load time from a statement list.

    Imports nested in functions are lazy edges and stay out; ``if __name__``
    and ``TYPE_CHECKING`` blocks never run on import, so they stay out too.
    """
    for node in body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("grokbuild."):
                    edges.add(alias.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom):
            if node.module == "grokbuild":
                edges.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith("grokbuild."):
                edges.add(node.module.split(".")[1])
        elif isinstance(node, ast.Try):
            for block in (node.body, node.orelse, node.finalbody, *(h.body for h in node.handlers)):
                _top_level_imports(block, edges)
        elif isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "__name__" in test or "TYPE_CHECKING" in test:
                continue
            _top_level_imports(node.body, edges)
            _top_level_imports(node.orelse, edges)


def test_top_level_import_graph_is_acyclic() -> None:
    """Load-time imports between grokbuild modules form a DAG.

    Importability alone does not prove this: a cycle can survive behind import
    order and only bite when a module is imported first from a new entry point.
    """
    modules = {path.stem: path for path in (ROOT / "grokbuild").glob("*.py")}
    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        edges: set[str] = set()
        _top_level_imports(ast.parse(path.read_text(encoding="utf-8")).body, edges)
        graph[name] = (edges & modules.keys()) - {name}
    colour: dict[str, int] = {}
    cycles: list[list[str]] = []

    def visit(node: str, stack: list[str]) -> None:
        colour[node] = 1
        stack.append(node)
        for target in sorted(graph.get(node, ())):
            if colour.get(target) == 1:
                cycles.append(stack[stack.index(target) :] + [target])
            elif target not in colour:
                visit(target, stack)
        stack.pop()
        colour[node] = 2

    for name in sorted(graph):
        if name not in colour:
            visit(name, [])
    assert not cycles, "load-time import cycles: " + "; ".join(" -> ".join(c) for c in cycles)


def _module_bindings(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return names


def _internal_references(tree: ast.Module) -> set[str]:
    """Module-level names settlement's own function bodies read."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            names.update(
                n.id
                for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            )
    return names & _module_bindings(tree)


def test_hook_patches_reach_settlement() -> None:
    """A test that patches ``hook.<name>`` must not silently miss settlement.

    ``hook.py`` hands a fixed set of callbacks to ``settlement.py`` through
    ``settlement.configure``; everything else settlement uses it imports
    directly, so a monkeypatch on the hook namespace never reaches it. A test
    exercising a hook entry point that delegates to settlement must therefore
    patch the settlement name as well - otherwise the patch is vacuum and the
    real function runs under the test's nose.
    """
    settlement_tree = ast.parse((ROOT / "grokbuild" / "settlement.py").read_text(encoding="utf-8"))
    hook_source = (ROOT / "grokbuild" / "hook.py").read_text(encoding="utf-8")
    seam = set(re.findall(r"^\s+(\w+)=_settlement_callback\(", hook_source, re.MULTILINE))
    assert seam, "the settlement seam must be declared in hook.py"
    reachable = _internal_references(settlement_tree) - seam
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            on_hook: set[str] = set()
            on_settlement: set[str] = set()
            for call in (n for n in ast.walk(function) if isinstance(n, ast.Call)):
                func = call.func
                if not (isinstance(func, ast.Attribute) and func.attr == "setattr"):
                    continue
                if len(call.args) < 2 or not isinstance(call.args[1], ast.Constant):
                    continue
                target = call.args[0]
                if not isinstance(target, ast.Name):
                    continue
                if target.id in {"hook", "hook_route"}:
                    on_hook.add(str(call.args[1].value))
                elif target.id == "settlement":
                    on_settlement.add(str(call.args[1].value))
            missing = sorted((on_hook & reachable) - on_settlement)
            if missing:
                offenders.append(f"{path.name}::{function.name} patches hook.{missing} only")
    assert not offenders, "\n".join(offenders)
