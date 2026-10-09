"""Client-entry resolution used by the *Client setup* dialog (no QGIS required).

Regression pins for two real defects found while writing the usage guide:

* the dialog used to point at ``mcp/piterm_qgis_mcp/min.py`` (the file is
  ``minimal.py``) and derived the repo root by counting ``dirname`` levels — which
  silently produced ``<profile>/python`` once the plugin was installed;
* a broken path was pasted into client configs without any warning.
"""

from __future__ import annotations

import os

from piterm_qgis_ai.client_paths import (describe, find_checkout, looks_like_repo,
                                         min_script_in, resolve_entry)


def make_checkout(root):
    """Create a minimal directory tree that looks like the source checkout."""
    script = os.path.join(root, "mcp", "piterm_qgis_mcp", "minimal.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("# stub\n")
    with open(os.path.join(root, "pyproject.toml"), "w", encoding="utf-8") as fh:
        fh.write("[project]\n")
    server = os.path.join(root, "mcp", "piterm_qgis_mcp", "server.py")
    with open(server, "w", encoding="utf-8") as fh:
        fh.write("# stub\n")
    entry = os.path.join(root, "mcp", "piterm_qgis_mcp", "__main__.py")
    with open(entry, "w", encoding="utf-8") as fh:
        fh.write("# stub\n")
    return script


def make_bundle(plugin_dir):
    """Bundled stdlib copy only (no pyproject / server module)."""
    script = os.path.join(plugin_dir, "mcp", "piterm_qgis_mcp", "minimal.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("# stub\n")
    return script


def plugin_file_under(plugin_dir):
    ui = os.path.join(plugin_dir, "ui")
    os.makedirs(ui, exist_ok=True)
    path = os.path.join(ui, "configurator.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# stub\n")
    return path


def test_min_script_in_requires_the_real_filename(tmp_path):
    plugin = str(tmp_path / "plugin")
    os.makedirs(os.path.join(plugin, "mcp", "piterm_qgis_mcp"), exist_ok=True)
    wrong = os.path.join(plugin, "mcp", "piterm_qgis_mcp", "min.py")
    with open(wrong, "w", encoding="utf-8") as fh:
        fh.write("# the old, wrong name\n")
    # min.py must NOT satisfy the resolver (this is defect D19).
    assert min_script_in(plugin) == ""
    make_bundle(plugin)
    assert min_script_in(plugin).endswith(os.path.join(
        "mcp", "piterm_qgis_mcp", "minimal.py"))


def test_find_checkout_walks_up(tmp_path):
    root = str(tmp_path / "checkout")
    script = make_checkout(root)
    deep = os.path.join(root, "plugin", "piterm_qgis_ai", "ui")
    os.makedirs(deep, exist_ok=True)
    found_root, found = find_checkout(deep)
    assert found == script
    assert os.path.samefile(found_root, root)


def test_looks_like_repo_needs_pyproject_and_server(tmp_path):
    root = str(tmp_path / "checkout")
    make_checkout(root)
    assert looks_like_repo(root)
    bundle = str(tmp_path / "bundle")
    make_bundle(bundle)
    assert not looks_like_repo(bundle)


def test_resolve_from_source_checkout(tmp_path):
    root = str(tmp_path / "checkout")
    script = make_checkout(root)
    plugin_file = plugin_file_under(os.path.join(root, "plugin", "piterm_qgis_ai"))
    entry = resolve_entry(plugin_file)
    assert entry["min_script"] == script
    assert entry["source"] == "checkout"
    assert entry["full_available"] is True
    assert os.path.samefile(entry["repo_root"], root)


def test_resolve_installed_profile_uses_bundle(tmp_path):
    """Installed layout: nothing but the plugin folder exists -> bundled copy."""
    plugin = str(tmp_path / "profiles" / "default" / "python" / "plugins"
                 / "piterm_qgis_ai")
    script = make_bundle(plugin)
    plugin_file = plugin_file_under(plugin)
    entry = resolve_entry(plugin_file)
    assert entry["min_script"] == script
    assert entry["source"] == "bundled"
    assert entry["repo_root"] == ""
    assert entry["full_available"] is False
    # the old dirname-counting bug produced <profile>/python/... -> must not win
    assert "profiles\\default\\python\\mcp" not in entry["min_script"]


def test_resolve_prefers_configured_checkout(tmp_path):
    bundle_root = str(tmp_path / "installed")
    plugin_file = plugin_file_under(bundle_root)
    make_bundle(bundle_root)
    checkout = str(tmp_path / "checkout")
    script = make_checkout(checkout)
    entry = resolve_entry(plugin_file, configured_root=checkout)
    assert entry["min_script"] == script
    assert entry["source"] == "configured"
    assert entry["full_available"] is True


def test_resolve_ignores_stale_configured_root(tmp_path):
    """A deleted checkout must fall back, not emit a dead path."""
    gone = str(tmp_path / "moved-away")
    plugin_file = plugin_file_under(str(tmp_path / "installed"))
    make_bundle(str(tmp_path / "installed"))
    entry = resolve_entry(plugin_file, configured_root=gone)
    assert entry["source"] == "bundled"
    assert os.path.isfile(entry["min_script"])


def test_resolve_extra_roots(tmp_path):
    plugin_file = plugin_file_under(str(tmp_path / "installed"))
    checkout = str(tmp_path / "elsewhere")
    script = make_checkout(checkout)
    entry = resolve_entry(plugin_file, extra_roots=(checkout,))
    assert entry["min_script"] == script
    assert entry["source"] == "checkout"


def test_resolve_missing_is_explicit(tmp_path):
    plugin_file = plugin_file_under(str(tmp_path / "empty-plugin"))
    entry = resolve_entry(plugin_file)
    assert entry["source"] == "missing"
    assert entry["min_script"] == ""
    assert entry["full_available"] is False
    text = describe(entry)
    assert "No MCP client script found" in text
    assert "install.ps1" in text


def test_describe_reports_full_mode_availability(tmp_path):
    root = str(tmp_path / "checkout")
    make_checkout(root)
    plugin_file = plugin_file_under(os.path.join(root, "plugin", "piterm_qgis_ai"))
    text = describe(resolve_entry(plugin_file))
    assert "source checkout" in text
    assert "unavailable" not in text
    bundle = str(tmp_path / "bundle")
    make_bundle(bundle)
    text2 = describe(resolve_entry(plugin_file_under(bundle)))
    assert "plugin-bundled copy" in text2
    assert "full FastMCP server unavailable" in text2
