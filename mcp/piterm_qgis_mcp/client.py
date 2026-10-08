"""Client re-exports (thin wrapper over the dependency-free ``minimal`` core)."""

from __future__ import annotations

from .minimal import BridgeClient, BridgeError, content_for, load_manifest, \
    mcp_tool_descriptor, tool_name_for, schema_for  # noqa: F401

__all__ = ["BridgeClient", "BridgeError", "content_for", "load_manifest",
           "mcp_tool_descriptor", "tool_name_for", "schema_for"]
