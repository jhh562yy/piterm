"""Entry point: ``python -m piterm_qgis_mcp``.

* no args / ``serve`` -> full FastMCP server (falls back to minimal stdio when
  the ``mcp`` package is missing);
* ``mcp-stdio`` -> dependency-free MCP stdio server;
* everything else (``ping`` / ``call`` / ``tools`` / ``screenshot`` / ``batch``)
  -> CLI from the minimal core.
"""

from __future__ import annotations

import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("serve", "mcp"):
        from .server import main as serve_main
        return serve_main(argv[1:] if argv else [])
    from .minimal import cli as minimal_cli
    return minimal_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
