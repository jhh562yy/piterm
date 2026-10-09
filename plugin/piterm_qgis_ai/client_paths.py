"""Locate the MCP client entry points used by the *Client setup* dialog.

The plugin ships only QGIS-side code; the MCP client/server live in the source
checkout. Where they are depends on how the plugin was installed:

* **from a checkout** — ``<repo>/mcp/piterm_qgis_mcp/minimal.py``;
* **installed into a profile** — the installer drops a stdlib copy at
  ``<plugin>/mcp/piterm_qgis_mcp/minimal.py`` and records the checkout path in
  the setting ``piterm_qgis_ai/meta.repo_root`` (only when it still exists).

Resolution order: configured root → walk up from the plugin file → walk up from
the extra roots → the bundled copy. Nothing here imports QGIS or the MCP
package, so the rules are unit-testable outside QGIS (``tests/test_client_paths.py``).
"""

from __future__ import annotations

import os

#: relative path of the zero-dependency client/server entry point
MIN_REL = os.path.join("mcp", "piterm_qgis_mcp", "minimal.py")
#: relative path of the fastmcp server module (only a checkout has it)
FULL_ENTRY_REL = os.path.join("mcp", "piterm_qgis_mcp", "__main__.py")
#: files that prove a directory is a runnable checkout
REPO_MARKERS = ("pyproject.toml",
                os.path.join("mcp", "piterm_qgis_mcp", "server.py"))
#: how many parent directories the upward search may climb
MAX_WALK_UP = 8


def min_script_in(root):
    """Return ``<root>/mcp/piterm_qgis_mcp/minimal.py`` when it exists, else ''."""
    if not root:
        return ""
    try:
        candidate = os.path.join(os.path.abspath(root), MIN_REL)
    except Exception:  # noqa: BLE001 - never let path weirdness break the dialog
        return ""
    return candidate if os.path.isfile(candidate) else ""


def looks_like_repo(root):
    """True when *root* is a source checkout able to serve the full MCP server."""
    if not root:
        return False
    try:
        root = os.path.abspath(root)
    except Exception:  # noqa: BLE001
        return False
    if not all(os.path.exists(os.path.join(root, marker))
               for marker in REPO_MARKERS):
        return False
    return os.path.isfile(os.path.join(root, FULL_ENTRY_REL))


def find_checkout(start, max_up=MAX_WALK_UP):
    """Climb from *start* until a directory holding the client script is found.

    Returns ``(root, script)`` or ``("", "")``. The starting directory itself is
    tested first, so an installed plugin finds its own bundled copy.
    """
    cur = os.path.abspath(start or ".")
    for _ in range(max(1, int(max_up))):
        script = min_script_in(cur)
        if script:
            return cur, script
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return "", ""


def _result(script, root, source):
    checkout = looks_like_repo(root) if root else False
    if source != "configured":
        if not root:
            source = "missing"
        else:
            source = "checkout" if checkout else "bundled"
    return {
        "min_script": script,
        "repo_root": root if checkout else "",
        "source": source,
        "full_available": checkout,
    }


def resolve_entry(plugin_file, configured_root="", extra_roots=()):
    """Resolve ``min_script`` / ``repo_root`` for the client-setup snippets.

    ``source`` is one of:

    * ``"configured"`` — taken from the recorded ``meta.repo_root`` setting;
    * ``"checkout"``   — a real checkout (pyproject + server module present);
    * ``"bundled"``    — only the stdlib client copy next to the plugin;
    * ``"missing"``    — nothing found (the dialog explains how to fix it).
    """
    try:
        plugin_dir = os.path.dirname(os.path.abspath(plugin_file))
    except Exception:  # noqa: BLE001
        plugin_dir = os.getcwd()

    if configured_root:
        script = min_script_in(configured_root)
        if script:
            return _result(script, os.path.abspath(configured_root), "configured")

    roots = [plugin_dir]
    for extra in extra_roots or ():
        if extra:
            try:
                roots.append(os.path.abspath(extra))
            except Exception:  # noqa: BLE001
                continue
    for root in roots:
        found_root, script = find_checkout(root)
        if script:
            return _result(script, found_root, "checkout")
    return _result("", "", "missing")


def describe(entry):
    """One-line human summary of a :func:`resolve_entry` result (for the dialog)."""
    source = entry.get("source")
    if source == "missing":
        return ("No MCP client script found. Re-run scripts\\install.ps1 "
                "(bundles a stdlib copy) or set 'meta.repo_root' to the checkout "
                "(system.config_set).")
    where = {"configured": "recorded repo root", "checkout": "source checkout",
             "bundled": "plugin-bundled copy"}.get(source, source)
    text = "client script: %s (%s)" % (entry.get("min_script"), where)
    if not entry.get("full_available"):
        text += " · full FastMCP server unavailable (checkout not found)"
    return text
