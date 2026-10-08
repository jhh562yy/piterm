"""Command registry: ``@command`` marks a handler method as reachable from the wire.

Why a registry and not a hand-built dict (learned from the reference
implementation, which rebuilt a 119-entry table on every dispatch):

* one place to declare a command: name, risk level, summary, tags;
* dispatch resolves ``getattr(server, spec.func_name)`` after membership check,
  so an attacker can never name arbitrary attributes (``stop``, ``_dispatch``);
* the registry is the single source for ``help``, ``hello`` capabilities and
  the generated ``docs/TOOLS.md`` - counts can never drift from reality.

Stdlib-only, importable outside QGIS (the docs generator parses sources with
AST, so it does not even need to import this module).
"""

from __future__ import annotations

import inspect
import threading

from .constants import RISK_LEVELS, RISK_TO_SCOPE


class ParamSpec:
    __slots__ = ("name", "default", "required", "type_hint", "doc")

    def __init__(self, name, default=None, required=True, type_hint="", doc=""):
        self.name = name
        self.default = default
        self.required = required
        self.type_hint = type_hint
        self.doc = doc

    def to_dict(self):
        d = {"name": self.name, "required": self.required, "type": self.type_hint}
        if not self.required:
            d["default"] = self.default
        if self.doc:
            d["doc"] = self.doc
        return d


class CommandSpec:
    __slots__ = ("name", "func_name", "module", "summary", "risk", "tags",
                 "params", "hidden", "examples", "returns")

    def __init__(self, name, func_name, module, summary, risk, tags,
                 params=None, hidden=False, examples=None, returns=""):
        self.name = name
        self.func_name = func_name
        self.module = module
        self.summary = summary
        self.risk = risk
        self.tags = tuple(tags)
        self.params = params or []
        self.hidden = hidden
        self.examples = examples or []
        self.returns = returns

    @property
    def scope(self):
        return RISK_TO_SCOPE.get(self.risk, "read")

    def to_dict(self, with_params=True):
        d = {
            "name": self.name,
            "summary": self.summary,
            "risk": self.risk,
            "tags": list(self.tags),
        }
        if with_params:
            d["params"] = [p.to_dict() for p in self.params]
        if self.returns:
            d["returns"] = self.returns
        if self.examples:
            d["examples"] = list(self.examples)
        return d


# ---------------------------------------------------------------------------
# Registry store (one per process; plugin and tests share it)
# ---------------------------------------------------------------------------
COMMANDS = {}
_LOCK = threading.Lock()


def describe_params(fn):
    """Best-effort parameter descriptions from the function signature.

    When a parameter has no annotation, the default value's type is used so MCP
    schemas still say ``boolean``/``integer`` instead of falling back to string
    (most handlers rely on defaults for their optional parameters).
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return []
    hints = {}
    try:
        hints = inspect.get_type_hints(fn, include_extras=False)
    except Exception:  # noqa: BLE001 - typing quirks must never break import
        hints = {}
    out = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "ctx"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        required = param.default is inspect.Parameter.empty
        hint = hints.get(pname, "")
        hint_str = getattr(hint, "__name__", None) or str(hint or "")
        if not hint_str or hint_str in ("Any", "typing.Any"):
            hint_str = _infer_type(param.default)
        out.append(ParamSpec(pname, None if required else _plain(param.default),
                             required, hint_str))
    return out


def _infer_type(default):
    if default is inspect.Parameter.empty or default is None:
        return "str"
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if isinstance(default, str):
        return "str"
    if isinstance(default, (list, tuple)):
        return "list"
    if isinstance(default, dict):
        return "dict"
    return "str"


def _plain(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def command(name=None, *, summary="", risk="read", tags=(), params=None,
            hidden=False, examples=(), returns=""):
    """Decorator registering a handler method as a wire command."""
    if risk not in RISK_LEVELS:
        raise ValueError("unknown risk level: %r" % (risk,))

    def deco(fn):
        cname = name or fn.__name__
        with _LOCK:
            if cname in COMMANDS:
                raise ValueError("duplicate command name: %s" % cname)
            spec = CommandSpec(
                name=cname,
                func_name=fn.__name__,
                module=getattr(fn, "__module__", ""),
                summary=summary or (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else summary,
                risk=risk,
                tags=tags,
                params=params if params is not None else describe_params(fn),
                hidden=hidden,
                examples=list(examples),
                returns=returns,
            )
            COMMANDS[cname] = spec
        fn._piterm_command = cname  # type: ignore[attr-defined]
        return fn
    return deco


def iter_specs(include_hidden=False):
    specs = list(COMMANDS.values())
    specs.sort(key=lambda s: s.name)
    if include_hidden:
        return specs
    return [s for s in specs if not s.hidden]


def command_count(include_hidden=False):
    return len(iter_specs(include_hidden))


def catalog(include_hidden=False, with_params=True):
    return [s.to_dict(with_params=with_params) for s in iter_specs(include_hidden)]
