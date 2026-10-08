"""Full MCP server (FastMCP) exposing every bridge command as an MCP tool.

Requires the ``mcp`` package (``pip install "piterm-qgis-mcp[full]"``). When it
is missing, ``__main__`` transparently falls back to the dependency-free
``minimal`` stdio server, so the bridge is always reachable.

The tool list is built from the live bridge (or the bundled manifest), which
means a plugin update automatically updates the MCP tool schemas - no drift.
"""

from __future__ import annotations

import inspect
import json
import os
import sys

from .minimal import BridgeClient, BridgeError, tool_name_for
from .registry import ToolRegistry

try:  # keep import-safe: the package must work without ``mcp`` installed
    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp >= 2.0
    MCP_AVAILABLE = True
except Exception:  # noqa: BLE001
    try:
        from mcp.server.fastmcp import FastMCP  # mcp < 2.0
        MCP_AVAILABLE = True
    except Exception:  # noqa: BLE001
        FastMCP = None  # type: ignore
        MCP_AVAILABLE = False

_TYPE_MAP = {
    "int": int, "integer": int, "float": float, "number": float,
    "bool": bool, "boolean": bool, "list": list, "array": list,
    "tuple": list, "dict": dict, "object": dict, "str": str, "string": str,
}


def build_client():
    return BridgeClient(
        host=os.environ.get("PITERM_QGIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("PITERM_QGIS_PORT", "9877") or 9877),
        token=os.environ.get("PITERM_QGIS_TOKEN", ""),
        timeout=float(os.environ.get("PITERM_QGIS_TIMEOUT", "60") or 60),
        client_name=os.environ.get("PITERM_QGIS_CLIENT", "piterm-mcp"),
        compress=os.environ.get("PITERM_QGIS_GZIP", "0").lower()
        not in ("0", "", "false", "no"),
    )


def _annotations(risk):
    if not MCP_AVAILABLE:
        return None
    try:
        from mcp.types import ToolAnnotations
    except Exception:  # noqa: BLE001
        return None
    try:
        if risk == "read":
            return ToolAnnotations(readOnlyHint=True)
        if risk == "write":
            return ToolAnnotations(readOnlyHint=False, idempotentHint=False)
        if risk in ("destructive", "execute"):
            return ToolAnnotations(readOnlyHint=False, destructiveHint=True)
    except Exception:  # noqa: BLE001
        return None
    return None


def _python_type(type_name):
    return _TYPE_MAP.get(str(type_name or "string"), str)


def _make_invoker(client, wire, params):
    parameters = []
    for param in params:
        name = str(param.get("name"))
        annotation = _python_type(param.get("type"))
        default = inspect.Parameter.empty if param.get("required", True) \
            else param.get("default", None)
        parameters.append(inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY, default=default,
            annotation=annotation))

    def invoke(**kwargs):
        cleaned = {k: v for k, v in kwargs.items() if v is not None}
        try:
            return client.call(wire, cleaned, timeout=_timeout_for(wire))
        except BridgeError as exc:
            raise RuntimeError(exc.to_text()) from exc

    invoke.__signature__ = inspect.Signature(parameters)
    invoke.__name__ = tool_name_for(wire)
    invoke.__doc__ = "Bridge command %s" % wire
    return invoke


def _timeout_for(wire):
    base = float(os.environ.get("PITERM_QGIS_TIMEOUT", "60") or 60)
    long_commands = ("processing.", "python.", "session.workflow_run",
                     "system.batch", "io.fetch_url", "canvas.render",
                     "layout.export", "layout.atlas_export", "layers.export",
                     "raster.export", "io.export_layer", "io.convert")
    if str(wire).startswith(long_commands):
        return max(base, 120.0)
    return base


def _register(mcp, name, description, fn, risk=None):
    kwargs = {"name": name, "description": description}
    annotations = _annotations(risk)
    if annotations is not None:
        kwargs["annotations"] = annotations
    try:
        return mcp.tool(**kwargs)(fn)
    except TypeError:
        kwargs.pop("annotations", None)
        return mcp.tool(**kwargs)(fn)


def create_server():
    if not MCP_AVAILABLE:
        raise RuntimeError("the 'mcp' package is required for the full server")

    client = build_client()
    mode = os.environ.get("PITERM_QGIS_TOOL_MODE", "granular").lower()
    registry = ToolRegistry(client, mode=mode).load()
    mcp = FastMCP("piterm-qgis")

    registered = 0
    if mode == "compound":
        for tool in registry.compound_tools():
            def make_compound(tool_spec):
                actions = tool_spec["actions"]

                def compound(action: str, params: dict = None):
                    wire = actions.get(action)
                    if wire is None:
                        raise RuntimeError(
                            "unknown action %r; valid: %s"
                            % (action, ", ".join(sorted(actions))))
                    try:
                        return client.call(wire, params or {},
                                           timeout=_timeout_for(wire))
                    except BridgeError as exc:
                        raise RuntimeError(exc.to_text()) from exc

                compound.__name__ = tool_spec["name"]
                compound.__doc__ = tool_spec["summary"]
                return compound

            _register(mcp, tool["name"], tool["summary"], make_compound(tool))
            registered += 1
    else:
        for tool in registry.granular_tools():
            invoker = _make_invoker(client, tool["wire"], tool["params"])
            description = tool["summary"] or tool["name"]
            if tool["risk"] != "read":
                description = "[%s] %s" % (tool["risk"], description)
            _register(mcp, tool["name"], description, invoker, risk=tool["risk"])
            registered += 1

    # ---- discovery / meta tools ---------------------------------------
    @mcp.tool(name="piterm_status",
              description="Bridge connection status, tool catalog source and "
                          "counts (read-only)")
    def piterm_status():
        return {
            "target": "%s:%s" % (client.host, client.port),
            "tool_catalog": registry.source,
            "tools_registered": registered,
            "tool_mode": mode,
            "client_version": "1.0.0",
        }

    @mcp.tool(name="piterm_tools",
              description="Search the bridge command catalog (read-only)")
    def piterm_tools(query: str = "", limit: int = 60):
        needle = (query or "").lower()
        matches = []
        for command in registry.commands:
            name = command.get("name", "")
            if needle and needle not in name.lower() and \
                    needle not in (command.get("summary") or "").lower():
                continue
            matches.append({"name": name, "summary": command.get("summary"),
                            "risk": command.get("risk"),
                            "params": [p.get("name") for p in
                                       command.get("params") or []]})
            if len(matches) >= max(1, min(int(limit), 400)):
                break
        return {"count": len(matches), "commands": matches}

    # ---- resources -----------------------------------------------------
    def _add_resource(uri, name, description, fetcher, mime="application/json"):
        def resource_fn():
            try:
                payload = fetcher()
                return json.dumps(payload, ensure_ascii=False)
            except BridgeError as exc:
                return json.dumps({"error": exc.to_text()})

        resource_fn.__name__ = "resource_%s" % re_safe(name)
        try:
            mcp.resource(uri, name=name, description=description,
                         mime_type=mime)(resource_fn)
        except TypeError:
            mcp.resource(uri, name=name, description=description)(resource_fn)

    _add_resource("qgis://info", "QGIS info",
                  "QGIS / plugin runtime info",
                  lambda: client.call("system.info"))
    _add_resource("qgis://project", "Project",
                  "Current project summary",
                  lambda: client.call("project.info"))
    _add_resource("qgis://layers", "Layers",
                  "Layer list",
                  lambda: client.call("layers.list", {"with_counts": False}))
    _add_resource("qgis://help", "Command catalog",
                  "Bridge command catalog",
                  lambda: {"commands": registry.commands})

    # ---- prompts -------------------------------------------------------
    @mcp.prompt(name="analyze_layer",
                description="Plan a quick data-quality pass over a layer")
    def analyze_layer(layer: str) -> str:
        return (
            "Use the Piterm QGIS tools to analyze layer '%s':\n"
            "1. layers.info to read schema, CRS and extent\n"
            "2. features.field_stats for every field\n"
            "3. features.query with limit=5 to eyeball rows\n"
            "4. report findings with concrete numbers, then suggest fixes."
            % layer)

    @mcp.prompt(name="make_map",
                description="Produce a styled map from a layer")
    def make_map(layer: str, field: str = "") -> str:
        target = ("graduated on field '%s'" % field) if field else "single symbol"
        return (
            "Make a map of layer '%s' (%s):\n"
            "1. layers.zoom_to\n"
            "2. style.renderer_set / style.labeling_set\n"
            "3. canvas.render and show the image\n"
            "4. save the project if the user asked for it." % (layer, target))

    return mcp


def re_safe(name):
    return "".join(ch if ch.isalnum() else "_" for ch in str(name))


def main(argv=None):
    if not MCP_AVAILABLE:
        sys.stderr.write(
            "The 'mcp' package is not installed; falling back to the minimal "
            "stdio server. Install with: pip install \"mcp>=1.0\"\n")
        from .minimal import cli as minimal_cli
        return minimal_cli(["mcp-stdio"])
    mcp = create_server()
    transport = os.environ.get("PITERM_QGIS_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http"):
        try:
            mcp.run(transport="streamable-http")
        except TypeError:
            mcp.run(transport="sse")
    else:
        mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
