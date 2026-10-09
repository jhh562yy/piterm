"""Installer regression pins (no QGIS required).

Two real defects found while (re)installing the plugin:

* ``scripts/install.ps1`` read the profile ini with ``Get-Content -Raw``, whose
  default is the ANSI code page. Qt writes those files as UTF-8, so every install
  re-encoded every non-ASCII value into mojibake and the damage compounded
  (a model named 天地图矢量流域提取 had decayed into hundreds of bytes of garbage).
* backups were moved to ``piterm_qgis_ai.bak-<stamp>`` *inside* ``python/plugins``,
  where QGIS enumerates every directory as a plugin — the folders showed up in the
  Plugin Manager as broken duplicates and were recorded in
  ``[plugin-manager] seen-plugins``.

These tests exercise the Python installer (``install.py``), which must stay in step
with the PowerShell one: same bundled client, same ``meta.repo_root`` bookkeeping,
same byte-preserving ini edit.
"""

from __future__ import annotations

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_installer():
    path = os.path.join(ROOT, "install.py")
    spec = importlib.util.spec_from_file_location("piterm_install", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = _load_installer()

CHINESE = "文本文件 (*.txt *.csv *.dat *.wkt *.tsv)"
MODEL = "model:天地图矢量流域提取4.3, grass:r.thin"


def write_ini(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (
        "[Cad]\r\n"
        "AngleShowInFloater=true\r\n"
        "[Plugin-DelimitedText]\r\n"
        "file_filter=%s\r\n"
        "[gui]\r\n"
        "processing\\recentAlgorithms=%s\r\n"
        "[PythonPlugins]\r\n"
        "db_manager=true\r\n"
        "processing=true\r\n"
    ) % (CHINESE, MODEL)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return content


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def test_enable_keeps_non_ascii_and_line_endings(tmp_path):
    ini = str(tmp_path / "QGIS" / "QGIS3.ini")
    write_ini(ini)
    install._enable_in_ini(ini, repo_root=r"G:\AIato\pi\piterm-qgis-ai")
    text = read(ini)

    # the values Qt wrote must survive verbatim (this is what used to rot)
    assert CHINESE in text
    assert MODEL in text
    # CRLF must not be rewritten to LF by a universal-newline round trip
    assert text.count("\n") == text.count("\r\n"), "line endings were rewritten"
    # ... and the plugin bookkeeping must be there
    assert "piterm_qgis_ai=true" in text
    assert "meta.repo_root=G:/AIato/pi/piterm-qgis-ai" in text


def test_enable_is_idempotent(tmp_path):
    ini = str(tmp_path / "QGIS" / "QGIS3.ini")
    write_ini(ini)
    install._enable_in_ini(ini, repo_root=r"G:\AIato\pi\piterm-qgis-ai")
    first = read(ini)
    install._enable_in_ini(ini, repo_root=r"G:\AIato\pi\piterm-qgis-ai")
    assert read(ini) == first, "a second install changed the ini"


def test_enable_handles_lf_ini(tmp_path):
    """A file QGIS never touched (LF) must stay LF, not gain CRs."""
    ini = str(tmp_path / "QGIS" / "QGIS4.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    with open(ini, "w", encoding="utf-8", newline="") as fh:
        fh.write("[PythonPlugins]\ndb_manager=true\nfile_filter=%s\n" % CHINESE)
    install._enable_in_ini(ini, repo_root="/tmp/checkout")
    text = read(ini)
    assert "\r" not in text
    assert CHINESE in text
    assert "meta.repo_root=/tmp/checkout" in text


def _profile(tmp_path, app="QGIS3"):
    profile_dir = tmp_path / app / "profiles" / "default"
    return {
        "app": app,
        "name": "default",
        "path": str(profile_dir),
        "plugins": str(profile_dir / "python" / "plugins"),
        "ini": str(profile_dir / "QGIS" / ("%s.ini" % app)),
    }


def test_install_bundles_client_and_keeps_backups_out_of_plugins(tmp_path):
    profile = _profile(tmp_path)
    write_ini(profile["ini"])

    # first install: nothing to back up
    install.install_profile(profile)
    target = os.path.join(profile["plugins"], "piterm_qgis_ai")
    assert os.path.isfile(os.path.join(target, "mcp", "piterm_qgis_mcp",
                                       "minimal.py")), "bundled client missing"
    assert os.path.isfile(os.path.join(target, "manifest.json"))
    assert os.path.isfile(os.path.join(target, "metadata.txt"))

    # second install: the previous copy must be backed up OUTSIDE python/plugins
    install.install_profile(profile)
    inside = [n for n in os.listdir(profile["plugins"]) if ".bak" in n]
    assert inside == [], "backup landed in python/plugins: %s" % inside
    backup_root = os.path.join(profile["path"], "_piterm_backups")
    assert os.path.isdir(backup_root)
    assert any(n.startswith("piterm_qgis_ai.bak-") for n in os.listdir(backup_root))

    # the ini backup stays next to the ini (it is not in a scan path either)
    ini_dir = os.path.dirname(profile["ini"])
    assert any(n.startswith("QGIS3.ini.bak-piterm-") for n in os.listdir(ini_dir))


def test_uninstall_disables_and_clears_repo_root(tmp_path):
    profile = _profile(tmp_path)
    write_ini(profile["ini"])
    install.install_profile(profile)
    install.uninstall_profile(profile)
    target = os.path.join(profile["plugins"], "piterm_qgis_ai")
    assert not os.path.exists(target)
    text = read(profile["ini"])
    assert "piterm_qgis_ai=false" in text
    assert "meta.repo_root=" in text
    assert "meta.repo_root=G:/" not in text
