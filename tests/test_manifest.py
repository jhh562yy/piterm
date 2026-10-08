"""Registry manifest tests: count, uniqueness, risk vocabulary.

Parses the handler sources with AST so it runs without QGIS and still proves the
registry can never silently lose commands. Also asserts the generated
``manifest.json`` (when present) matches the sources.
"""

from __future__ import annotations

import ast
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HANDLERS = os.path.join(ROOT, "plugin", "piterm_qgis_ai", "handlers")
MANIFEST = os.path.join(ROOT, "mcp", "piterm_qgis_mcp", "manifest.json")
VALID_RISKS = {"read", "write", "destructive", "execute"}
MIN_COMMANDS = 120


def _literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        return None


def collect_commands():
    commands = {}
    func_names = {}
    for filename in sorted(os.listdir(HANDLERS)):
        if not filename.endswith(".py") or filename in ("__init__.py", "base.py"):
            continue
        path = os.path.join(HANDLERS, filename)
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                decorator_name = getattr(func, "id", None) or getattr(func, "attr", None)
                if decorator_name != "command":
                    continue
                kwargs = {kw.arg: _literal(kw.value) for kw in decorator.keywords}
                name = kwargs.get("name") or node.name
                risk = kwargs.get("risk") or "read"
                commands[name] = {
                    "module": filename,
                    "function": node.name,
                    "risk": risk,
                    "summary": kwargs.get("summary") or "",
                }
                func_names.setdefault(node.name, []).append(filename)
    return commands, func_names


def test_command_count_and_names():
    commands, _ = collect_commands()
    assert len(commands) >= MIN_COMMANDS, (
        "expected at least %d commands, found %d" % (MIN_COMMANDS, len(commands)))
    for name in commands:
        assert "." in name, "command %r should be domain.verb" % name
        assert name == name.lower(), "command %r must be lowercase" % name


def test_risks_valid():
    commands, _ = collect_commands()
    for name, meta in commands.items():
        assert meta["risk"] in VALID_RISKS, (name, meta["risk"])


def test_function_names_unique_across_modules():
    _commands, func_names = collect_commands()
    duplicates = {name: files for name, files in func_names.items()
                  if len(files) > 1}
    assert not duplicates, "duplicate handler method names: %s" % duplicates


def test_every_module_contributes():
    commands, _ = collect_commands()
    modules = {meta["module"] for meta in commands.values()}
    expected = {"system.py", "project.py", "layers.py", "features.py",
                "raster.py", "processing.py", "canvas.py", "style.py",
                "layout.py", "session.py", "io.py", "connections.py",
                "plugins.py", "expression.py", "python_exec.py", "ui.py"}
    missing = expected - modules
    assert not missing, "handler modules without commands: %s" % missing


def test_manifest_matches_sources_when_present():
    if not os.path.exists(MANIFEST):
        return  # generated during the QGIS test phase
    with open(MANIFEST, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest_names = {c["name"] for c in manifest["commands"]}
    commands, _ = collect_commands()
    assert manifest_names == set(commands), (
        "manifest drift: only-in-manifest=%s only-in-source=%s"
        % (sorted(manifest_names - set(commands))[:10],
           sorted(set(commands) - manifest_names)[:10]))
