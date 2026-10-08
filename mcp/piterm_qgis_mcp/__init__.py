"""Piterm QGIS MCP server package.

Two personalities, one source of truth:

* ``minimal`` - a single-file, dependency-free client + MCP stdio server;
* ``server``  - the full FastMCP server (requires the ``mcp`` package) that
  exposes every bridge command as a first-class MCP tool with rich schemas,
  resources and prompts.

Both read the exact same tool catalog: the live bridge's
``system.capabilities`` when QGIS is running, or the bundled
``manifest.json`` otherwise.
"""

from __future__ import annotations

__version__ = "1.0.0"
