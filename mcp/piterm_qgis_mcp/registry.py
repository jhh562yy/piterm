"""Tool registry: turn the bridge command catalog into MCP tool specs.

Sources, in order of preference:

1. the live bridge (``system.capabilities``) - always accurate for the running
   plugin build;
2. ``manifest.json`` next to this package - generated from the plugin registry;
3. a tiny builtin fallback so the server still starts when nothing else works.

The registry also builds *compound* tools (one tool with an ``action``
parameter per domain) for clients with tight tool-count limits.
"""

from __future__ import annotations

import collections

from .minimal import BridgeClient, BridgeError, load_manifest, tool_name_for


class ToolRegistry:
    def __init__(self, client: BridgeClient, mode="granular"):
        self.client = client
        self.mode = mode
        self.commands = []
        self.source = "none"

    def load(self):
        try:
            payload = self.client.call("system.capabilities")
            commands = payload.get("commands") or []
            if commands:
                self.commands = commands
                self.source = "live"
                return self
        except BridgeError:
            pass
        except Exception:  # noqa: BLE001 - never let discovery kill startup
            pass
        self.commands = load_manifest()
        self.source = "manifest"
        return self

    # ------------------------------------------------------------------
    def granular_tools(self):
        out = []
        for command in self.commands:
            out.append({
                "wire": command.get("name"),
                "name": tool_name_for(command.get("name", "")),
                "summary": command.get("summary") or "",
                "risk": command.get("risk") or "read",
                "tags": command.get("tags") or [],
                "params": command.get("params") or [],
            })
        return out

    def compound_tools(self):
        groups = collections.OrderedDict()
        for command in self.commands:
            tags = command.get("tags") or ["misc"]
            tag = tags[0]
            groups.setdefault(tag, []).append(command)
        out = []
        for tag, commands in groups.items():
            actions = {command["name"].split(".", 1)[-1]: command["name"]
                       for command in commands}
            if not actions:
                continue
            out.append({
                "name": "piterm_%s" % tag,
                "summary": "Piterm %s commands (%d actions)" % (tag, len(actions)),
                "actions": actions,
                "risk": "mixed",
            })
        return out

    def tools(self):
        if self.mode == "compound":
            return self.compound_tools()
        return self.granular_tools()
