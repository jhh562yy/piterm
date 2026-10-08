"""Unit tests for the coverage script's sandbox interlock and helpers.

The interlock is what keeps ``--sandbox-db`` from ever touching operator
profiles: QGIS's settings root must live under ``_scratch/cov_sandbox``.
These tests pin that logic (and the small path/JSON helpers) without QGIS.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_coverage_module():
    path = os.path.join(ROOT, "scripts", "qgis_command_coverage.py")
    spec = importlib.util.spec_from_file_location("qgis_command_coverage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cov = _load_coverage_module()


def test_norm_path_collapses_separators_and_case():
    a = cov._norm_path("G:\\AIato\\pi\\_scratch\\cov_sandbox\\")
    b = cov._norm_path("g:/aiato/pi/_scratch/cov_sandbox")
    assert a == b
    assert cov._norm_path(None) == ""
    assert cov._norm_path("") == ""


def test_norm_path_normalizes_dot_segments():
    a = cov._norm_path(os.path.join(cov.SANDBOX_DIR, "cfg_344", "..", "cfg_344"))
    assert a == cov._norm_path(os.path.join(cov.SANDBOX_DIR, "cfg_344"))


def test_inside_sandbox_accepts_redirected_root():
    env_root = os.path.join(cov.SANDBOX_DIR, "cfg_344")
    settings = os.path.join(env_root, "profiles", "default")
    assert cov._inside_sandbox(settings, env_root)
    # trailing separators on either side must not matter
    assert cov._inside_sandbox(settings + os.sep, env_root + os.sep)


def test_inside_sandbox_requires_env_var():
    settings = os.path.join(cov.SANDBOX_DIR, "cfg_423", "profiles", "default")
    assert not cov._inside_sandbox(settings, "")
    assert not cov._inside_sandbox(settings, None)


def test_inside_sandbox_rejects_user_profiles():
    user = r"C:\Users\someone\AppData\Roaming\QGIS\QGIS3\profiles\default"
    assert not cov._inside_sandbox(user, r"C:\Users\someone\AppData\Roaming\QGIS")
    assert not cov._inside_sandbox(cov.SANDBOX_DIR, os.path.dirname(cov.SANDBOX_DIR))


def test_inside_sandbox_rejects_prefix_tricks():
    # a sibling directory that merely *starts with* the sandbox name is not
    # inside it (the check requires a path-separator boundary)
    evil = cov.SANDBOX_DIR + "_evil"
    assert not cov._inside_sandbox(os.path.join(evil, "profiles", "default"),
                                   evil)


def test_inside_sandbox_rejects_mismatched_env_root():
    # settings inside the sandbox area, but the env var points at another
    # sandbox slot: refuse (both ends must agree)
    settings = os.path.join(cov.SANDBOX_DIR, "cfg_344", "profiles", "default")
    other = os.path.join(cov.SANDBOX_DIR, "cfg_999")
    assert not cov._inside_sandbox(settings, other)


def test_short_serializes_and_truncates():
    assert cov._short({"a": 1}) == '{"a": 1}'
    long = cov._short("x" * 500, limit=10)
    assert len(long) <= 10
    # non-serializable values fall back to str()
    assert cov._short(object()) is not None


def test_cleanup_gpkg_removes_file_and_sidecars():
    with tempfile.TemporaryDirectory(prefix="piterm_cov_test_") as tmp:
        gpkg = os.path.join(tmp, "x.gpkg")
        for name in (gpkg, gpkg + "-wal", gpkg + "-journal"):
            with open(name, "w", encoding="utf-8") as fh:
                fh.write("stub")
        out = cov._cleanup_gpkg(gpkg)
        assert not os.path.exists(gpkg)
        assert not os.path.exists(gpkg + "-wal")
        assert not os.path.exists(gpkg + "-journal")
        assert "x.gpkg" in out["removed"]


def test_cleanup_gpkg_missing_path_is_ok():
    with tempfile.TemporaryDirectory(prefix="piterm_cov_test_") as tmp:
        out = cov._cleanup_gpkg(os.path.join(tmp, "never-existed.gpkg"))
        assert out["removed"] == []
