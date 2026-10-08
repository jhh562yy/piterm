"""Code execution: a sandboxed ``python.exec`` (default) and a deliberately
gated ``python.exec_full``.

Sandbox rules (mirrors and extends the GeoAgent approach):

* imports: only ``qgis`` and ``math`` (top-level package match, so ``mathutils``
  cannot slip through);
* no ``eval/exec/compile/open/input/__import__`` calls, no dunder attribute
  access, no ``__builtins__``/``__globals__``/``__subclasses__`` names;
* a restricted builtins table; prepared namespace with ``iface``, ``project``,
  ``canvas``, ``active_layer``;
* stdout captured and *always* restored (a leaked redirect would swallow QGIS's
  own output for the rest of the session);
* wall-clock deadline enforced with a trace hook for pure-Python loops.
"""

from __future__ import annotations

import ast
import contextlib
import io
import time
import traceback

from ..errors import CommandError, ErrorCode
from ..registry import command
from .base import HandlerBase

_ALLOWED_IMPORT_ROOTS = {"qgis", "math"}
_BLOCKED_CALLS = {"eval", "exec", "compile", "open", "input", "__import__",
                  "globals", "locals", "vars", "getattr", "setattr", "delattr",
                  "breakpoint", "exit", "quit", "help", "memoryview",
                  "type", "object", "super", "classmethod", "staticmethod"}
_BLOCKED_NAMES = {"__builtins__", "__dict__", "__globals__", "__subclasses__",
                  "__loader__", "__spec__", "__file__"}

_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "int": int,
    "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "range": range, "reversed": reversed,
    "round": round, "set": set, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "zip": zip, "print": print,
}


class _DeadlineTracer:
    def __init__(self, deadline):
        self.deadline = deadline
        self.armed = True

    def trace(self, frame, event, arg):
        if self.armed and event == "line" and time.monotonic() > self.deadline:
            raise TimeoutError("python.exec exceeded its deadline")
        return self.trace


class PythonHandlers(HandlerBase):

    @command(name="python.exec", summary="Run sandboxed Python (qgis/math "
             "imports only, no file/desktop escape hatches)", risk="execute",
             tags=("python", "sandbox"))
    def python_exec(self, code, timeout=30, capture_variables=True):
        return self._execute(code, timeout, capture_variables, sandbox=True)

    @command(name="python.exec_full", summary="Run arbitrary Python with full "
             "builtins (requires dangerous.allow_full_python)", risk="execute",
             tags=("python", "dangerous"))
    def python_exec_full(self, code, timeout=30, capture_variables=True):
        return self._execute(code, timeout, capture_variables, sandbox=False)

    # ------------------------------------------------------------------
    def _execute(self, code, timeout, capture_variables, sandbox):
        text = str(code or "")
        if not text.strip():
            raise CommandError(ErrorCode.EXEC, "empty code")
        if sandbox:
            _validate_sandbox(text)
        namespace = self._namespace(sandbox)
        before = set(namespace.keys())
        stdout = io.StringIO()
        deadline = time.monotonic() + float(timeout or 30)
        tracer = _DeadlineTracer(deadline)
        import sys
        started = time.monotonic()
        compiled = compile(text, "<piterm-python>", "exec")
        error_payload = None
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
                sys.settrace(tracer.trace)
                try:
                    exec(compiled, namespace)
                finally:
                    sys.settrace(None)
        except TimeoutError as exc:
            error_payload = {"type": "TimeoutError", "message": str(exc),
                             "traceback": None}
        except BaseException as exc:  # noqa: BLE001 - report everything
            error_payload = {
                "type": type(exc).__name__,
                "message": str(exc)[:2000],
                "traceback": traceback.format_exc()[-3000:],
            }
        elapsed = round(time.monotonic() - started, 3)
        output = stdout.getvalue()
        if error_payload is not None:
            raise CommandError(ErrorCode.EXEC,
                               "%s: %s" % (error_payload["type"],
                                           error_payload["message"]),
                               detail={"stdout": output[-4000:],
                                       "traceback": error_payload["traceback"],
                                       "elapsed": elapsed})
        result = {"stdout": output[-8000:], "elapsed": elapsed,
                  "deadline_hit": time.monotonic() > deadline}
        if capture_variables:
            new_names = [name for name in namespace.keys()
                         if name not in before and not name.startswith("_")]
            variables = {}
            for name in sorted(new_names)[:60]:
                value = namespace[name]
                try:
                    text_repr = repr(value)
                except Exception:  # noqa: BLE001
                    text_repr = "<unprintable>"
                variables[name] = {"type": type(value).__name__,
                                   "repr": text_repr[:300]}
            result["variables"] = variables
        return result

    def _namespace(self, sandbox):
        namespace = {}
        if sandbox:
            def limited_import(name, globals=None, locals=None, fromlist=(),
                               level=0):
                root = str(name).split(".")[0]
                if level != 0 or root not in _ALLOWED_IMPORT_ROOTS:
                    raise ImportError("import of %r is not allowed in the "
                                      "sandbox" % name)
                return __import__(name, globals, locals, fromlist, level)

            builtins_table = dict(_SAFE_BUILTINS)
            builtins_table["__import__"] = limited_import
            namespace["__builtins__"] = builtins_table
        namespace["iface"] = self.iface
        namespace["project"] = self.project
        try:
            namespace["canvas"] = self.canvas
        except Exception:  # noqa: BLE001
            namespace["canvas"] = None
        try:
            namespace["active_layer"] = self.iface.activeLayer() if self.iface else None
        except Exception:  # noqa: BLE001
            namespace["active_layer"] = None
        return namespace


# ---------------------------------------------------------------------------
# AST gate
# ---------------------------------------------------------------------------
def _validate_sandbox(code):
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise CommandError(ErrorCode.EXEC, "invalid syntax: %s" % exc)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_import(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise CommandError(ErrorCode.EXEC,
                                   "relative imports are not allowed")
            _check_import(node.module or "")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _BLOCKED_CALLS:
                raise CommandError(ErrorCode.EXEC,
                                   "calling %r is not allowed in the sandbox"
                                   % node.func.id,
                                   detail={"hint": "use python.exec_full with "
                                                   "dangerous.allow_full_python "
                                                   "if you really need it"})
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise CommandError(ErrorCode.EXEC,
                                   "dunder attribute access is not allowed")
        elif isinstance(node, ast.Name):
            if node.id in _BLOCKED_NAMES:
                raise CommandError(ErrorCode.EXEC,
                                   "access to %r is not allowed" % node.id)


def _check_import(name):
    root = str(name).split(".")[0]
    if root not in _ALLOWED_IMPORT_ROOTS:
        raise CommandError(ErrorCode.EXEC,
                           "import of %r is not allowed in the sandbox "
                           "(qgis and math only)" % name)
