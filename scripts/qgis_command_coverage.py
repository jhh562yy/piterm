"""Command coverage sweep - headless QGIS.

Complements qgis_headless_smoke.py (63 checks): this script systematically
exercises every bridge command that is safe in a throwaway headless session -
the whole data / io / layout / raster / session-editor / plugins / project /
python-sandbox surface. Canvas/UI/live-only commands are covered by
``_scratch/live_coverage.py`` against a running GUI instead.

Checks are graded:

* ``hard``   - must succeed (or fail with the expected error code);
* ``soft``   - executed and recorded, but a failure does not fail the run
               (use for environment-dependent paths, e.g. Processing provider
               quirks, atlas export, plugin start helpers).

Writes ``verification/logs/coverage_<qgis>_<stamp>.json`` and exits non-zero
when any hard check fails.

By default the two connection-writing commands (db.connection_add,
db.import_layer) are skipped: they would rewrite the saved-connections
settings. ``--sandbox-db`` runs them for real against a throwaway settings
root - the flag refuses to execute unless QGIS_CUSTOM_CONFIG_PATH redirects
QgsApplication.qgisSettingsDirPath() inside ``_scratch/cov_sandbox``. The
same run gives db.tables / db.sql / db.add_layer their first positive
exercise, against a generated GeoPackage.

    "C:\\Program Files\\QGIS 3.44.15\\bin\\python-qgis-ltr.bat" scripts/qgis_command_coverage.py [--sandbox-db]
"""

from __future__ import annotations

import faulthandler
import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time

faulthandler.enable()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# overridable so several instances can be swept in parallel when needed
FRAME_PORT = int(os.environ.get("PITERM_COV_PORT", "19887"))
HTTP_PORT = int(os.environ.get("PITERM_COV_HTTP_PORT", "19889"))
FETCH_PORT = int(os.environ.get("PITERM_COV_FETCH_PORT", "19881"))

# commands deliberately exercised by the live-GUI sweep instead (no canvas /
# no UI / real plugin-folder state in a headless throwaway session)
LIVE_ONLY = [
    "canvas.center_set", "canvas.extent_get", "canvas.extent_set",
    "canvas.refresh", "canvas.render", "canvas.render_3d",
    "canvas.rotation_set", "canvas.scale_get", "canvas.scale_set",
    "canvas.screenshot", "canvas.zoom_features", "canvas.zoom_full",
    "features.identify", "features.zoom_selection", "layers.zoom_to",
    "ui.message", "ui.open_attribute_table",
]
# skipped by default (by design): they would rewrite the user's saved
# database connections; their read paths are exercised instead. With
# --sandbox-db they run for real against a throwaway settings root and drop
# out of this list.
SKIPPED_BY_DESIGN = ["db.connection_add", "db.import_layer"]
# throwaway QGIS settings root used by --sandbox-db (outside the repo)
SANDBOX_DIR = os.path.join(os.path.dirname(ROOT), "_scratch", "cov_sandbox")


def _client_setup(bridge):
    """Exercise system.client_setup and assert the resolved entry is usable.

    Catches the class of defect where the Client-setup dialog hands a client a
    path that does not exist (wrong file name / wrong root).
    """
    payload = bridge.call("system.client_setup", {"client": "vscode"})
    entry = payload.get("entry") or {}
    clients = payload.get("clients") or {}
    assert clients, "no client snippets returned"
    assert "vscode" in clients, "requested client missing from the catalog"
    assert payload.get("selected", {}).get("content"), "selected snippet empty"
    script = entry.get("min_script") or ""
    assert entry.get("source") != "missing", "client entry point was not resolved"
    assert script.lower().endswith("minimal.py"), (
        "client entry should be the stdlib client (minimal.py), got %r" % script)
    assert os.path.isfile(script), "resolved client script does not exist: %r" % script
    return {"source": entry.get("source"), "clients": len(clients),
            "full_available": bool(entry.get("full_available")),
            "min_script": script}


class Report:
    def __init__(self):
        self.ok = True
        self.checks = []
        self.executed = set()

    def _record(self, name, ok, entry):
        self.checks.append(entry)
        base = name.split("#")[0]
        self.executed.add(base)
        tag = "soft" if entry.get("soft") else ("expect" if entry.get("expected_error") else "ok")
        status = "ok" if ok else "FAIL"
        print("[cov] %-40s %-6s %s" % (name, tag, status
                                       + ("" if ok else ": " + entry.get("error", "")[:160])),
              flush=True)

    def check(self, name, fn, expect_error=None, soft=False):
        started = time.perf_counter()
        try:
            value = fn()
            if expect_error and not soft:
                raise AssertionError("expected error %s, got %r"
                                     % (expect_error, _short(value)))
            entry = {"name": name, "ok": True,
                     "ms": round((time.perf_counter() - started) * 1000, 1),
                     "value": _short(value)}
            if soft:
                entry["soft"] = True
            self._record(name, True, entry)
            return value
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            if expect_error and code == expect_error:
                entry = {"name": name, "ok": True, "expected_error": expect_error,
                         "ms": round((time.perf_counter() - started) * 1000, 1)}
                self._record(name, True, entry)
                return None
            entry = {"name": name, "ok": False,
                     "ms": round((time.perf_counter() - started) * 1000, 1),
                     "error": "%s: %s" % (type(exc).__name__, exc)}
            detail = getattr(exc, "detail", None)
            if detail:
                entry["detail"] = _short(detail, 500)
            if soft:
                entry["soft"] = True
                self._record(name, False, entry)
                return None
            self.ok = False
            self._record(name, False, entry)
            return None

    def refusal(self, name, fn):
        """Negative test: the command MUST refuse the call (any bridge error)."""
        started = time.perf_counter()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            entry = {"name": name, "ok": True, "refused": True,
                     "code": getattr(exc, "code", None),
                     "ms": round((time.perf_counter() - started) * 1000, 1)}
            self._record(name, True, entry)
            return None
        entry = {"name": name, "ok": False,
                 "error": "expected a refusal but the call succeeded",
                 "ms": round((time.perf_counter() - started) * 1000, 1)}
        self.ok = False
        self._record(name, False, entry)
        return None


def _short(value, limit=300):
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text[:limit]


def _assert(value, message="assertion failed"):
    if not value:
        raise AssertionError(message)
    return value


def _norm_path(path):
    text = str(path or "")
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text)).replace("\\", "/")


def _inside_sandbox(settings_dir, env_root):
    """True only when QGIS_CUSTOM_CONFIG_PATH redirected the settings root
    into the throwaway _scratch/cov_sandbox area."""
    if not env_root:
        return False
    settings = _norm_path(settings_dir)
    return (settings.startswith(_norm_path(SANDBOX_DIR) + "/")
            and settings.startswith(_norm_path(env_root) + "/"))


def _make_empty_gpkg(path):
    """(Re)create an empty GeoPackage through QGIS itself.

    Raw osgeo calls proved unreliable inside a QGIS process (created files fail
    to re-open through the bundled GDAL python bindings), so the file is made
    with QgsVectorFileWriter from a scratch layer, then that scratch layer is
    dropped through the OGR provider connection."""
    from qgis.core import (QgsProject, QgsProviderRegistry,
                           QgsVectorFileWriter, QgsVectorLayer)
    for sibling in (path, path + "-wal", path + "-journal", path + "-shm"):
        if os.path.exists(sibling):
            try:
                os.remove(sibling)
            except OSError:
                pass
    scratch = QgsVectorLayer("Point?crs=EPSG:4326&field=tmp:int",
                             "piterm_scratch", "memory")
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = "piterm_scratch"
    options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
    err, message, _out, _layer = QgsVectorFileWriter.writeAsVectorFormatV3(
        scratch, path, QgsProject.instance().transformContext(), options)
    if err != QgsVectorFileWriter.NoError:
        raise AssertionError("could not create gpkg (rc=%s): %s" % (err, message))
    connection = QgsProviderRegistry.instance().providerMetadata(
        "ogr").createConnection(path, {})
    connection.dropVectorTable("", "piterm_scratch")
    layers = [t.tableName() for t in connection.tables("")]
    if layers:
        raise AssertionError("fresh gpkg still has %s" % layers)
    return {"gpkg": path, "layers": 0, "bytes": os.path.getsize(path)}


def _archive_gpkg(path):
    """Copy the populated sandbox GeoPackage into verification/logs."""
    from qgis.core import Qgis
    if not os.path.exists(path):
        return None
    dest = os.path.join(ROOT, "verification", "logs",
                        "cov_sandbox_import_%d.gpkg" % Qgis.QGIS_VERSION_INT)
    shutil.copy2(path, dest)
    return dest


def _sandbox_setting_path():
    from qgis.core import QgsSettings
    return QgsSettings().value("GeoPackage/connections/piterm_cov_gpkg/path",
                               None)


def _sandbox_evidence(gpkg):
    """Durable readback: settings file used, file state, archived copy."""
    from qgis.core import QgsSettings
    settings = QgsSettings()
    try:
        settings_file = settings.fileName()
    except Exception:  # noqa: BLE001
        settings_file = None
    return {
        "settings_file": settings_file,
        "gpkg_path_setting_after_remove": _sandbox_setting_path(),
        "gpkg_exists": os.path.exists(gpkg),
        "gpkg_bytes": os.path.getsize(gpkg) if os.path.exists(gpkg) else 0,
        "archive": _archive_gpkg(gpkg),
    }


def _cleanup_gpkg(path):
    removed = []
    deadline = time.time() + 5
    while True:
        for sibling in (path, path + "-journal", path + "-wal", path + "-shm"):
            if os.path.exists(sibling):
                try:
                    os.remove(sibling)
                    removed.append(os.path.basename(sibling))
                except OSError:
                    pass
        if not os.path.exists(path) or time.time() > deadline:
            break
        time.sleep(0.2)
    if os.path.exists(path):
        raise AssertionError("gpkg still present after cleanup: %s" % removed)
    return {"removed": removed}


def main():
    sys.path.insert(0, os.path.join(ROOT, "plugin"))
    sys.path.insert(0, os.path.join(ROOT, "mcp"))

    from qgis.core import Qgis, QgsApplication
    from qgis.PyQt.QtCore import QTimer

    app_name = ("QGIS4-piterm-cov" if Qgis.QGIS_VERSION_INT >= 40000
                else "QGIS3-piterm-cov")
    QgsApplication.setOrganizationName("QGIS")
    QgsApplication.setOrganizationDomain("qgis.org")
    QgsApplication.setApplicationName(app_name)
    app = QgsApplication([], False)
    app.setApplicationName(app_name)
    app.initQgis()

    sandbox = {
        "requested": "--sandbox-db" in sys.argv[1:],
        "active": _inside_sandbox(QgsApplication.qgisSettingsDirPath(),
                                  os.environ.get("QGIS_CUSTOM_CONFIG_PATH", "")),
    }

    report = Report()
    report_data = {"ok": True, "qgis": Qgis.QGIS_VERSION, "checks": []}
    tmp = tempfile.mkdtemp(prefix="piterm_cov_")
    state = {"timeout": False}
    server = None
    fetch_httpd = None

    try:
        from piterm_qgis_ai.checkpoints import Checkpoints
        from piterm_qgis_ai.cursors import CursorManager
        from piterm_qgis_ai.handlers import build_hub
        from piterm_qgis_ai.jobs import JobManager
        from piterm_qgis_ai.journal import Journal
        from piterm_qgis_ai.security import DictSettings
        from piterm_qgis_ai.server import PitermServer

        hub = build_hub(journal=Journal(), jobs=JobManager(),
                        cursors=CursorManager(),
                        checkpoints=Checkpoints(os.path.join(tmp, "ckpt")))
        settings = DictSettings({
            "mode.host": "127.0.0.1",
            "mode.allow_execute": True,
            "paths.allow": ["*"],
            "audit.enabled": False,
            "audit.path": os.path.join(tmp, "audit.jsonl"),
        })
        server = PitermServer(hub, iface=None, settings_provider=settings,
                              plugin_version="1.0.0")
        status = report.check("server.start",
                              lambda: server.start("127.0.0.1", FRAME_PORT,
                                                   HTTP_PORT, True))
        if status and status.get("errors"):
            raise SystemExit("server errors: %s" % status["errors"])

        # local file server for io.fetch_url (loopback only)
        class _Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D102
                pass

        handler = functools.partial(_Quiet, directory=tmp)
        fetch_httpd = http.server.ThreadingHTTPServer(("127.0.0.1", FETCH_PORT), handler)
        threading.Thread(target=fetch_httpd.serve_forever, daemon=True).start()

        from piterm_qgis_mcp.minimal import BridgeClient

        client = BridgeClient("127.0.0.1", FRAME_PORT, client_name="coverage")
        client_long = BridgeClient("127.0.0.1", FRAME_PORT,
                                   client_name="coverage-long", timeout=120)

        def run_checks():
            try:
                run_coverage(report, client, client_long, tmp, app, sandbox)
            except Exception as exc:  # noqa: BLE001
                report.ok = False
                import traceback
                report_data["fatal"] = "%s: %s\n%s" % (
                    type(exc).__name__, exc, traceback.format_exc()[-2000:])
            finally:
                state["worker_done"] = True

        QTimer.singleShot(300, lambda: threading.Thread(target=run_checks,
                                                        daemon=True).start())
        safety = QTimer()
        safety.setSingleShot(True)
        safety.setInterval(240000)
        safety.timeout.connect(lambda: (state.update({"timeout": True}),
                                        app.quit()))
        safety.start()
        poller = QTimer()
        poller.setInterval(150)
        poller.timeout.connect(lambda: app.quit() if state.get("worker_done") else None)
        poller.start()
        app.exec()
        try:
            client.close()
            client_long.close()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        report.ok = False
        import traceback
        report_data["fatal"] = "%s: %s\n%s" % (type(exc).__name__, exc,
                                               traceback.format_exc()[-2000:])
    finally:
        try:
            if fetch_httpd is not None:
                fetch_httpd.shutdown()
                fetch_httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if server is not None:
                server.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            app.exitQgis()
        except Exception:  # noqa: BLE001
            pass

    report_data["checks"] = report.checks
    report_data["ok"] = bool(report.ok and not state["timeout"])

    # coverage accounting against the manifest
    try:
        with open(os.path.join(ROOT, "mcp", "piterm_qgis_mcp", "manifest.json"),
                  encoding="utf-8") as fh:
            manifest = json.load(fh)
        names = [c["name"] for c in manifest["commands"]]
    except Exception:  # noqa: BLE001
        names = []
    executed = {n for n in names if n in report.executed}
    not_exercised = [n for n in names if n not in executed]
    report_data["coverage"] = {
        "manifest_total": len(names),
        "exercised_headless": len(executed),
        "soft_failures": sorted({c["name"] for c in report.checks
                                 if c.get("soft") and not c.get("ok")}),
        "not_exercised_headless": not_exercised,
        "live_only": LIVE_ONLY,
        "sandbox_db": sandbox,
        # commands really executed in sandbox mode drop out of this list
        "skipped_by_design": [n for n in SKIPPED_BY_DESIGN
                              if n not in report.executed],
    }

    logs_dir = os.path.join(ROOT, "verification", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(logs_dir, "coverage_%s_%s.json"
                       % (report_data["qgis"].replace(" ", "_"), stamp))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report_data, fh, ensure_ascii=False, indent=1)
    passed = sum(1 for c in report_data["checks"] if c.get("ok"))
    failed = sum(1 for c in report_data["checks"] if not c.get("ok"))
    print("COVERAGE %s | passed=%d failed=%d | exercised=%d/%d | %s"
          % ("OK" if report_data["ok"] else "FAIL", passed, failed,
             len(executed), len(names), out))
    for entry in report_data["checks"]:
        if not entry.get("ok"):
            print("  %s %s: %s" % ("SOFT" if entry.get("soft") else "FAIL",
                                   entry["name"], entry.get("error", "")[:200]))
    return 0 if report_data["ok"] else 1


def run_coverage(report, C, CL, tmp, app, sandbox=None):
    """Execute the coverage matrix. ``C`` = normal client, ``CL`` = long-timeout."""
    sandbox = sandbox or {}
    from piterm_qgis_mcp.minimal import BridgeClient

    # ---------------------------------------------------------------- fixtures
    def add_points():
        return C.call("layers.add_memory", {
            "name": "cov_points", "geometry_type": "point", "crs": "EPSG:4326",
            "fields": [{"name": "name", "type": "string"},
                       {"name": "value", "type": "double"}]})

    report.check("fixture.add_points", add_points)
    report.check("fixture.insert_points", lambda: C.call("features.insert", {
        "layer": "cov_points",
        "features": [
            {"attributes": {"name": "a", "value": 1.5},
             "geometry": {"wkt": "POINT (1 1)"}},
            {"attributes": {"name": "b", "value": 3.0},
             "geometry": {"wkt": "POINT (2 2)"}},
            {"attributes": {"name": "c", "value": 4.5},
             "geometry": {"wkt": "POINT (3 3)"}},
            {"attributes": {"name": "d", "value": 6.0},
             "geometry": {"wkt": "POINT (4 4)"}},
            {"attributes": {"name": "e", "value": 7.5},
             "geometry": {"wkt": "POINT (5 5)"}},
        ]}))
    report.check("fixture.add_poly", lambda: C.call("layers.add_memory", {
        "name": "cov_poly", "geometry_type": "polygon", "crs": "EPSG:4326",
        "fields": [{"name": "name", "type": "string"}]}))
    report.check("fixture.insert_poly", lambda: C.call("features.insert", {
        "layer": "cov_poly",
        "features": [{"attributes": {"name": "zone"},
                      "geometry": {"wkt": "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))"}}]}))
    report.check("fixture.add_join", lambda: C.call("layers.add_memory", {
        "name": "cov_join", "geometry_type": "point", "crs": "EPSG:4326",
        "fields": [{"name": "name", "type": "string"}]}))
    report.check("fixture.insert_join", lambda: C.call("features.insert", {
        "layer": "cov_join",
        "features": [{"attributes": {"name": "a"}, "geometry": {"wkt": "POINT (0 0)"}},
                     {"attributes": {"name": "b"}, "geometry": {"wkt": "POINT (0 1)"}}]}))
    report.check("io.write_text", lambda: C.call("io.write_text", {
        "path": os.path.join(tmp, "cov.csv"), "text": "name,x,y\na,1,1\nb,2,2\n"}))
    shutil.copy(os.path.join(ROOT, "verification", "live_map.png"),
                os.path.join(tmp, "cov_logo.png"))

    # ------------------------------------------------------- layers (extended)
    report.check("layers.crs_get", lambda: C.call("layers.crs_get", {"layer": "cov_points"}))
    report.check("layers.extent", lambda: C.call("layers.extent", {"layer": "cov_points"}))
    report.check("layers.fields_list", lambda: C.call("layers.fields_list", {"layer": "cov_points"}))
    report.check("layers.active_get", lambda: C.call("layers.active_get"))
    report.check("layers.active_set", lambda: C.call("layers.active_set", {"layer": "cov_points"}), soft=True)
    report.check("layers.find", lambda: C.call("layers.find", {"pattern": "cov"}))
    report.check("layers.visibility", lambda: C.call("layers.visibility",
                                                     {"layer": "cov_poly", "visible": False}))
    report.check("layers.subset_filter_set", lambda: C.call(
        "layers.subset_filter_set", {"layer": "cov_points", "filter": "\"value\" > 0"}))
    report.check("layers.subset_filter_clear", lambda: C.call(
        "layers.subset_filter_clear", {"layer": "cov_points"}))
    report.check("layers.rename", lambda: C.call(
        "layers.rename", {"layer": "cov_join", "name": "cov_join2"}))
    report.check("layers.joins_add", lambda: C.call("layers.joins_add", {
        "layer": "cov_points", "join_layer": "cov_join2", "join_field": "name",
        "target_field": "name", "prefix": "j_"}))
    report.check("layers.joins_list", lambda: C.call(
        "layers.joins_list", {"layer": "cov_points"}))
    report.check("layers.joins_remove", lambda: C.call("layers.joins_remove", {
        "layer": "cov_points", "join_layer": "cov_join2"}))
    report.check("layers.export", lambda: C.call("layers.export", {
        "layer": "cov_points", "path": os.path.join(tmp, "cov_points.gpkg")}))
    report.check("layers.export.exists", lambda: _assert(
        os.path.exists(os.path.join(tmp, "cov_points.gpkg")), "gpkg missing"))
    report.check("layers.duplicate", lambda: C.call(
        "layers.duplicate", {"layer": "cov_points", "new_name": "cov_dup"}))
    report.check("layers.fields_rename", lambda: C.call(
        "layers.fields_rename", {"layer": "cov_dup", "name": "value", "new_name": "val2"}))
    report.check("layers.fields_delete", lambda: C.call(
        "layers.fields_delete", {"layer": "cov_dup", "name": "val2"}))
    report.check("layers.crs_set", lambda: C.call(
        "layers.crs_set", {"layer": "cov_dup", "crs": "EPSG:3857"}), soft=True)
    report.check("layers.set_source", lambda: C.call(
        "layers.set_source", {"layer": "cov_dup",
                              "source": os.path.join(tmp, "cov_points.gpkg")}), soft=True)
    report.check("layers.source", lambda: C.call("layers.source", {"layer": "cov_dup"}))
    report.check("layers.opacity", lambda: C.call(
        "layers.opacity", {"layer": "cov_dup", "opacity": 0.5}))
    report.check("layers.set_property", lambda: C.call(
        "layers.set_property", {"layer": "cov_dup", "opacity": 0.3,
                                "min_scale": 1000}), soft=True)
    order_ids = report.check("layers.order.list", lambda: [
        item["id"] for item in C.call("layers.list")["layers"]])
    if order_ids:
        report.check("layers.order", lambda: C.call(
            "layers.order", {"layer_ids": list(reversed(order_ids))}))
    report.check("layers.group_create", lambda: C.call(
        "layers.group_create", {"name": "cov_group"}))
    report.check("layers.group_move_layer", lambda: C.call(
        "layers.group_move_layer", {"layer": "cov_dup", "group": "cov_group"}))
    report.check("layers.group_remove", lambda: C.call(
        "layers.group_remove", {"group": "cov_group", "also_remove_layers": True}))
    report.check("layers.add_vector", lambda: C.call("layers.add_vector", {
        "path": os.path.join(tmp, "cov_points.gpkg"), "name": "cov_from_gpkg"}))
    report.check("layers.add_delimited", lambda: C.call("layers.add_delimited", {
        "path": os.path.join(tmp, "cov.csv"), "x_field": "x", "y_field": "y",
        "name": "cov_csv"}))
    report.check("layers.add_web", lambda: C.call("layers.add_web", {
        "url": "http://127.0.0.1:%d/{z}/{x}/{y}.png" % FETCH_PORT,
        "kind": "xyz", "name": "cov_xyz"}), soft=True)
    report.check("layers.remove", lambda: C.call(
        "layers.remove", {"layer": "cov_from_gpkg"}))

    # ----------------------------------------------------- features (extended)
    report.check("features.get", lambda: C.call(
        "features.get", {"layer": "cov_points", "fid": 1}))
    report.check("features.update_geometry", lambda: C.call(
        "features.update_geometry", {"layer": "cov_points", "updates": [
            {"fid": 2, "geometry": {"wkt": "POINT (8 8)"}}]}))
    report.check("features.spatial_query", lambda: C.call(
        "features.spatial_query", {"layer": "cov_points", "predicate": "intersects",
                                   "geometry": {"wkt": "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))"}}))
    report.check("features.nearest", lambda: C.call(
        "features.nearest", {"layer": "cov_points", "x": 1.05, "y": 1.05, "k": 2}))
    report.check("features.selection_clear", lambda: C.call(
        "features.selection_clear", {"layer": "cov_points"}))
    report.check("features.cursors_list", lambda: C.call("features.cursors_list"))
    report.check("features.delete", lambda: C.call(
        "features.delete", {"layer": "cov_join2", "fids": [1]}))
    report.check("features.field_stats", lambda: C.call(
        "features.field_stats", {"layer": "cov_points", "field": "value",
                                 "stats": ["count", "mean", "max"]}))

    # -------------------------------------------------------------- expression
    report.check("expression.functions_search", lambda: C.call(
        "expression.functions_search", {"query": "geom", "limit": 5}))

    # ------------------------------------------------------------------- style
    report.check("style.classification_breaks", lambda: C.call(
        "style.classification_breaks", {"layer": "cov_points", "field": "value",
                                        "classes": 3}))
    report.check("style.labeling_set", lambda: C.call(
        "style.labeling_set", {"layer": "cov_points", "enabled": True, "field": "name"}))
    report.check("style.labeling_get", lambda: C.call(
        "style.labeling_get", {"layer": "cov_points"}))
    report.check("style.symbol_set", lambda: C.call(
        "style.symbol_set", {"layer": "cov_points", "color": "#ff0000", "size": 3}))
    report.check("style.copy", lambda: C.call(
        "style.copy", {"source_layer": "cov_points", "target_layer": "cov_poly"}))
    report.check("style.qml_save", lambda: C.call(
        "style.qml_save", {"layer": "cov_points", "path": os.path.join(tmp, "cov.qml")}))
    report.check("style.qml_apply", lambda: C.call(
        "style.qml_apply", {"layer": "cov_csv", "path": os.path.join(tmp, "cov.qml")}))

    # ----------------------------------------------------------------- session
    report.check("session.edit_begin", lambda: C.call(
        "session.edit_begin", {"layer": "cov_points"}))
    report.check("features.update", lambda: C.call("features.update", {
        "layer": "cov_points", "updates": [{"fid": 3, "attributes": {"name": "c2"}}]}))
    report.check("session.edit_commit", lambda: C.call(
        "session.edit_commit", {"layer": "cov_points"}))
    report.check("session.undo", lambda: C.call(
        "session.undo", {"layer": "cov_points", "steps": 1}))
    report.check("session.redo", lambda: C.call(
        "session.redo", {"layer": "cov_points", "steps": 1}))
    report.check("session.edit_begin#2", lambda: C.call(
        "session.edit_begin", {"layer": "cov_points"}))
    report.check("session.edit_rollback", lambda: C.call(
        "session.edit_rollback", {"layer": "cov_points"}))
    report.check("session.journal_list", lambda: C.call(
        "session.journal_list", {"since_seq": 0, "limit": 20}))
    report.check("session.journal_clear", lambda: C.call("session.journal_clear"))
    ckpt = report.check("session.checkpoint_create", lambda: C.call(
        "session.checkpoint_create", {"name": "cov_ck"}))
    ckpt_id = ckpt.get("checkpoint") if isinstance(ckpt, dict) else None
    if ckpt_id:
        report.check("session.checkpoint_restore", lambda: C.call(
            "session.checkpoint_restore", {"checkpoint": ckpt_id}), soft=True)
        report.check("session.checkpoint_delete", lambda: C.call(
            "session.checkpoint_delete", {"checkpoint": ckpt_id}), soft=True)

    # ---------------------------------------------------------------- raster
    def make_raster():
        out = os.path.join(tmp, "cov.tif")
        try:
            CL.call("processing.execute", {
                "algorithm": "native:createconstantrasterlayer",
                "parameters": {"EXTENT": "0,10,0,10 [EPSG:4326]",
                               "TARGET_CRS": "EPSG:4326", "PIXEL_SIZE": 0.5,
                               "NUMBER": 5, "OUTPUT": out}})
        except Exception:  # noqa: BLE001
            CL.call("processing.execute", {
                "algorithm": "gdal:rasterize",
                "parameters": {"INPUT": "cov_points", "BURN": 1, "UNITS": 1,
                               "WIDTH": 20, "HEIGHT": 20,
                               "EXTENT": "0,10,0,10 [EPSG:4326]",
                               "OUTPUT": out}})
        return os.path.exists(out)

    report.check("fixture.raster", make_raster)
    report.check("layers.add_raster", lambda: C.call("layers.add_raster", {
        "path": os.path.join(tmp, "cov.tif"), "name": "cov_raster"}))
    report.check("raster.info", lambda: C.call(
        "raster.info", {"layer": "cov_raster", "with_stats": True}), soft=True)
    report.check("raster.stats", lambda: C.call(
        "raster.stats", {"layer": "cov_raster", "band": 1}))
    report.check("raster.histogram", lambda: C.call(
        "raster.histogram", {"layer": "cov_raster", "band": 1, "bins": 8}))
    report.check("raster.renderer_info", lambda: C.call(
        "raster.renderer_info", {"layer": "cov_raster"}))
    report.check("raster.minmax_set", lambda: C.call(
        "raster.minmax_set", {"layer": "cov_raster", "band": 1, "min": 0, "max": 10}))
    report.check("raster.colormap_set", lambda: C.call("raster.colormap_set", {
        "layer": "cov_raster",
        "entries": [{"value": 0, "color": "#000000", "label": "low"},
                    {"value": 10, "color": "#ff0000", "label": "high"}]}), soft=True)
    report.check("raster.colormap_get", lambda: C.call(
        "raster.colormap_get", {"layer": "cov_raster"}))
    report.check("raster.sample", lambda: C.call("raster.sample", {
        "layer": "cov_raster", "points": [{"x": 1, "y": 1}, {"x": 5, "y": 5}]}), soft=True)
    report.check("raster.export", lambda: C.call("raster.export", {
        "layer": "cov_raster", "path": os.path.join(tmp, "cov_export.tif")}))
    report.check("raster.export.exists", lambda: _assert(
        os.path.exists(os.path.join(tmp, "cov_export.tif")), "tif missing"))
    report.check("raster.calculator", lambda: C.call("raster.calculator", {
        "expression": "A@1 * 2", "layers": [{"layer": "cov_raster", "var": "A"}],
        "output": os.path.join(tmp, "calc.tif")}), soft=True)
    report.check("raster.zonal_stats", lambda: CL.call("raster.zonal_stats", {
        "zones": "cov_poly", "raster": "cov_raster",
        "statistics": ["mean", "max"], "output": "memory:cov_zonal",
        "load_result": True}), soft=True)

    # ------------------------------------------------------------ processing
    report.check("processing.algorithms_get", lambda: C.call(
        "processing.algorithms_get", {"algorithm": "native:buffer"}))
    report.check("processing.batch_execute", lambda: CL.call(
        "processing.batch_execute", {"algorithm": "native:buffer", "parameter_sets": [
            {"INPUT": "cov_points", "DISTANCE": 1.0, "OUTPUT": "memory:pb1"},
            {"INPUT": "cov_points", "DISTANCE": 2.0, "OUTPUT": "memory:pb2"}]}))
    report.check("processing.models_list", lambda: C.call("processing.models_list"))
    report.refusal("processing.models_run", lambda: C.call("processing.models_run", {
        "model": "not_a_model"}))

    # -------------------------------------------------------------- jobs extra
    started = report.check("job.start.setup", lambda: C.call("processing.start", {
        "algorithm": "native:buffer",
        "parameters": {"INPUT": "cov_points", "DISTANCE": 3.0,
                       "OUTPUT": "memory:jo"}}))
    job_id = None
    if started:
        job_id = started.get("id") or started.get("job")
    if job_id is None:
        listing = C.call("job.list", {"limit": 1})
        if listing.get("jobs"):
            job_id = listing["jobs"][0]["id"]
    if job_id:
        deadline = time.time() + 40
        while time.time() < deadline:
            got = C.call("job.get", {"id": job_id, "with_log": True, "log_limit": 10})
            if got.get("state") in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.3)
        report.check("job.get", lambda: C.call("job.get", {"id": job_id}))
        report.check("job.log", lambda: C.call("job.log", {"id": job_id, "limit": 10}))
    report.refusal("job.cancel", lambda: C.call(
        "job.cancel", {"id": "no-such-job"}))
    report.check("job.clear", lambda: C.call("job.clear"))

    # ------------------------------------------------------------------- io
    report.check("io.convert", lambda: C.call("io.convert", {
        "path_in": os.path.join(tmp, "cov_points.gpkg"),
        "path_out": os.path.join(tmp, "cov_converted.geojson"),
        "kind": "vector"}), soft=True)
    report.check("io.import_file", lambda: C.call("io.import_file", {
        "path": os.path.join(tmp, "cov_converted.geojson")}), soft=True)
    report.check("io.fetch_url", lambda: C.call("io.fetch_url", {
        "url": "http://127.0.0.1:%d/cov.csv" % FETCH_PORT,
        "path": os.path.join(tmp, "cov_fetched.csv"), "timeout": 10}))
    def fetched_ok():
        with open(os.path.join(tmp, "cov_fetched.csv"), encoding="utf-8") as fh:
            text = fh.read()
        return _assert(text == "name,x,y\na,1,1\nb,2,2\n",
                       "fetched content differs")
    report.check("io.fetch_url.content", fetched_ok)

    # ---------------------------------------------------------------- layout
    report.check("layout.create", lambda: C.call("layout.create", {"name": "cov_layout"}))
    report.check("layout.list", lambda: C.call("layout.list"))
    report.check("layout.add_map", lambda: C.call("layout.add_map", {
        "name": "cov_layout", "x": 10, "y": 10, "width": 180, "height": 120}))
    report.check("layout.add_legend", lambda: C.call("layout.add_legend", {
        "name": "cov_layout", "x": 10, "y": 135, "width": 60, "height": 40}))
    report.check("layout.add_scalebar", lambda: C.call("layout.add_scalebar", {
        "name": "cov_layout", "x": 100, "y": 160, "width": 60, "height": 12}))
    label = report.check("layout.add_label", lambda: C.call("layout.add_label", {
        "name": "cov_layout", "text": "Coverage [% @project_title %]",
        "x": 10, "y": 132}))
    report.check("layout.add_picture", lambda: C.call("layout.add_picture", {
        "name": "cov_layout", "x": 170, "y": 135, "width": 20, "height": 20,
        "path": os.path.join(tmp, "cov_logo.png")}), soft=True)
    report.check("layout.add_shape", lambda: C.call("layout.add_shape", {
        "name": "cov_layout", "kind": "rectangle", "x": 5, "y": 5,
        "width": 200, "height": 140}), soft=True)
    report.check("layout.add_table", lambda: C.call("layout.add_table", {
        "name": "cov_layout", "layer": "cov_points", "x": 100, "y": 10,
        "width": 85, "height": 45, "max_features": 3}), soft=True)
    report.check("layout.info", lambda: C.call("layout.info", {"name": "cov_layout"}))
    if label:
        item_id = label.get("item")
        if item_id:
            report.check("layout.item_move_resize", lambda: C.call(
                "layout.item_move_resize", {"name": "cov_layout",
                                            "item_id": item_id, "x": 12, "y": 133}))
    report.check("layout.export", lambda: C.call("layout.export", {
        "name": "cov_layout", "path": os.path.join(tmp, "cov_layout.pdf"),
        "format": "pdf"}))
    report.check("layout.export.exists", lambda: _assert(
        os.path.exists(os.path.join(tmp, "cov_layout.pdf")), "pdf missing"))
    report.check("layout.export.png", lambda: C.call("layout.export", {
        "name": "cov_layout", "path": os.path.join(tmp, "cov_layout.png"),
        "format": "png", "dpi": 120}), soft=True)
    report.check("layout.export.png.exists", lambda: _assert(
        os.path.exists(os.path.join(tmp, "cov_layout.png")), "png missing"),
        soft=True)
    report.check("layout.atlas_configure", lambda: C.call("layout.atlas_configure", {
        "name": "cov_layout", "coverage_layer": "cov_poly", "enabled": True,
        "page_name_field": "name"}), soft=True)
    report.check("layout.atlas_export", lambda: C.call("layout.atlas_export", {
        "name": "cov_layout", "path": os.path.join(tmp, "cov_atlas.pdf"),
        "format": "pdf"}), soft=True)
    report.check("layout.remove", lambda: C.call("layout.remove", {"name": "cov_layout"}))

    # ------------------------------------------------------------- project
    report.check("project.metadata_get", lambda: C.call("project.metadata_get"))
    report.check("project.metadata_set", lambda: C.call("project.metadata_set", {
        "title": "Coverage sweep", "abstract": "command coverage", "keywords": "cov"}))
    bookmark = report.check("project.bookmarks_add", lambda: C.call(
        "project.bookmarks_add", {"name": "cov_bm", "bbox": [0, 0, 10, 10]}), soft=True)
    report.check("project.bookmarks_list", lambda: C.call("project.bookmarks_list"))
    if bookmark:
        bm_id = bookmark.get("id")
        if bm_id:
            report.check("project.bookmarks_zoom", lambda: C.call(
                "project.bookmarks_zoom", {"id": bm_id}), soft=True)
            report.check("project.bookmarks_remove", lambda: C.call(
                "project.bookmarks_remove", {"id": bm_id}))
    report.check("project.themes_add", lambda: C.call(
        "project.themes_add", {"name": "cov_theme"}), soft=True)
    report.check("project.themes_list", lambda: C.call("project.themes_list"))
    report.check("project.themes_apply", lambda: C.call(
        "project.themes_apply", {"name": "cov_theme"}), soft=True)
    report.check("project.themes_remove", lambda: C.call(
        "project.themes_remove", {"name": "cov_theme"}))

    def var_roundtrip():
        C.call("project.variables_set", {"key": "cov_var", "value": "1"})
        return C.call("project.variables_remove", {"key": "cov_var"})
    report.check("project.variables_list", lambda: C.call("project.variables_list"))
    report.check("project.variables_remove", var_roundtrip)
    report.check("project.set_crs", lambda: C.call(
        "project.set_crs", {"crs": "EPSG:3857"}), soft=True)
    proj_path = os.path.join(tmp, "cov_proj.qgz")
    report.check("project.save", lambda: C.call("project.save", {"path": proj_path}))
    report.check("project.new", lambda: C.call("project.new"))
    report.check("project.new.cleared", lambda: _assert(
        C.call("layers.list")["count"] == 0, "project.new did not clear layers"))
    report.check("project.open", lambda: C.call("project.open", {"path": proj_path}))
    report.check("project.open.loaded", lambda: _assert(
        C.call("layers.list")["count"] >= 1, "project.open did not load layers"))

    # ------------------------------------------------------------- python sandbox
    pos = report.check("python.exec", lambda: C.call("python.exec", {
        "code": "import math\nprint(round(math.pi, 3))"}))
    if pos:
        report.check("python.exec.result", lambda: _assert(
            "3.142" in pos.get("stdout", ""), "sandbox stdout wrong: %r" % pos))
    report.refusal("python.exec.blocks_import", lambda: C.call("python.exec", {
        "code": "import os"}))
    report.refusal("python.exec.blocks_open", lambda: C.call("python.exec", {
        "code": "open('x')"}))
    report.refusal("python.exec_full", lambda: C.call("python.exec_full", {
        "code": "print(1)"}))

    # ------------------------------------------------------------------- db
    report.check("db.connections_list", lambda: C.call("db.connections_list"))
    report.refusal("db.tables", lambda: C.call(
        "db.tables", {"connection": "nope"}))
    report.refusal("db.sql", lambda: C.call(
        "db.sql", {"connection": "nope", "sql": "select 1"}))
    report.refusal("db.add_layer", lambda: C.call(
        "db.add_layer", {"connection": "nope", "table": "t"}))
    report.check("db.connection_remove", lambda: C.call(
        "db.connection_remove", {"name": "nope"}), soft=True)

    # --------------------------------------- db sandbox (opt-in, --sandbox-db)
    # db.connection_add / db.import_layer would rewrite saved connections, so
    # they only run when QGIS_CUSTOM_CONFIG_PATH redirects the settings root
    # into _scratch/cov_sandbox. The same run gives db.tables / db.sql /
    # db.add_layer their first positive exercise on a generated GeoPackage.
    if sandbox.get("requested"):
        report.check("sandbox.interlock", lambda: _assert(
            sandbox.get("active"),
            "--sandbox-db needs QGIS_CUSTOM_CONFIG_PATH inside %s" % SANDBOX_DIR))
    if sandbox.get("requested") and sandbox.get("active"):
        db_dir = os.path.join(SANDBOX_DIR, "files")
        os.makedirs(db_dir, exist_ok=True)
        gpkg = os.path.join(db_dir, "cov_import.gpkg")
        report.check("sandbox.gpkg_fixture", lambda: _make_empty_gpkg(gpkg))
        # fresh source layers: memory layers reloaded from the saved project
        # come back empty (verified), so seed deterministic fixtures right here
        report.check("sandbox.source_layer", lambda: C.call("layers.add_memory", {
            "name": "cov_db_src", "geometry_type": "point", "crs": "EPSG:4326",
            "fields": [{"name": "name", "type": "string"},
                       {"name": "value", "type": "double"}]}))
        report.check("sandbox.source_seed", lambda: C.call("features.insert", {
            "layer": "cov_db_src",
            "features": [{"attributes": {"name": "s%d" % i, "value": float(i)},
                          "geometry": {"wkt": "POINT (%d %d)" % (i, i)}}
                         for i in range(5)]}))
        report.check("sandbox.source_layer2", lambda: C.call("layers.add_memory", {
            "name": "cov_db_src2", "geometry_type": "point", "crs": "EPSG:4326",
            "fields": [{"name": "name", "type": "string"}]}))
        report.check("sandbox.source_seed2", lambda: C.call("features.insert", {
            "layer": "cov_db_src2",
            "features": [{"attributes": {"name": "t%d" % i},
                          "geometry": {"wkt": "POINT (%d 0)" % i}}
                         for i in range(2)]}))
        report.check("db.connection_add", lambda: C.call("db.connection_add", {
            "name": "piterm_cov_gpkg", "kind": "gpkg", "path": gpkg}))
        report.check("db.connection_add#pg", lambda: C.call("db.connection_add", {
            "name": "piterm_cov_pg", "kind": "postgres", "host": "127.0.0.1",
            "port": 5432, "database": "piterm_cov"}))
        report.refusal("db.connection_add#invalid", lambda: C.call(
            "db.connection_add", {"name": "piterm_cov_bad", "kind": "postgres"}))
        report.check("db.connections_list#added", lambda: _assert(
            {"piterm_cov_gpkg", "piterm_cov_pg"} <= {c["name"] for c in
             C.call("db.connections_list")["connections"]},
            "sandbox connections missing from db.connections_list"))
        report.check("sandbox.settings_readback", lambda: _assert(
            _norm_path(_sandbox_setting_path()) == _norm_path(gpkg),
            "connection path not stored in the sandbox settings"))
        report.check("db.tables", lambda: C.call("db.tables", {
            "connection": "piterm_cov_gpkg", "kind": "gpkg"}))

        def imported_rows(expected):
            rows = C.call("db.sql", {
                "connection": "piterm_cov_gpkg", "kind": "gpkg",
                "sql": "select count(*) as n from cov_import"})["rows"]
            got = int(rows[0]["n"])
            return _assert(got == expected,
                           "imported row count != %d (got %s)"
                           % (expected, got))

        report.check("db.import_layer", lambda: C.call("db.import_layer", {
            "layer": "cov_db_src", "connection": "piterm_cov_gpkg",
            "table": "cov_import", "kind": "gpkg"}))
        report.refusal("db.import_layer#exists", lambda: C.call(
            "db.import_layer", {"layer": "cov_db_src",
                                "connection": "piterm_cov_gpkg",
                                "table": "cov_import", "kind": "gpkg"}))
        report.check("db.tables#imported", lambda: _assert(
            "cov_import" in [t["name"] for t in C.call(
                "db.tables", {"connection": "piterm_cov_gpkg",
                              "kind": "gpkg"})["tables"]],
            "cov_import not listed by db.tables"))
        report.check("db.sql#read", lambda: imported_rows(5))
        report.check("db.import_layer#overwrite", lambda: C.call(
            "db.import_layer", {"layer": "cov_db_src2",
                                "connection": "piterm_cov_gpkg",
                                "table": "cov_import", "kind": "gpkg",
                                "overwrite": True}))
        report.check("db.sql#read#overwrite", lambda: imported_rows(2))
        report.check("db.add_layer", lambda: C.call("db.add_layer", {
            "connection": "piterm_cov_gpkg", "kind": "gpkg",
            "table": "cov_import", "name": "cov_db_layer"}))
        report.check("db.add_layer#count", lambda: _assert(
            C.call("layers.count", {"layer": "cov_db_layer"})["count"] == 2,
            "db.add_layer feature count != 2"))
        report.check("db.connection_remove#gpkg", lambda: C.call(
            "db.connection_remove", {"name": "piterm_cov_gpkg", "kind": "gpkg"}))
        report.check("db.connection_remove#pg", lambda: C.call(
            "db.connection_remove", {"name": "piterm_cov_pg"}))
        report.check("db.connections_list#cleaned", lambda: _assert(
            not [c for c in C.call("db.connections_list")["connections"]
                 if str(c["name"]).startswith("piterm_cov")],
            "sandbox connections left behind"))
        report.check("sandbox.evidence", lambda: _sandbox_evidence(gpkg))
        # release the gpkg before deleting it: a loaded layer holds the file
        report.check("layers.remove#db", lambda: C.call(
            "layers.remove", {"layer": "cov_db_layer"}))
        report.check("layers.remove#db-src", lambda: C.call(
            "layers.remove", {"layer": "cov_db_src"}))
        report.check("layers.remove#db-src2", lambda: C.call(
            "layers.remove", {"layer": "cov_db_src2"}))
        report.check("sandbox.cleanup", lambda: _cleanup_gpkg(gpkg))

    # -------------------------------------------------------------- plugins
    import qgis.utils as qu
    dummy_dir = os.path.join(__import__("qgis.core", fromlist=["QgsApplication"])
                             .QgsApplication.qgisSettingsDirPath(),
                             "python", "plugins", "cov_dummy")
    try:
        os.makedirs(dummy_dir, exist_ok=True)
        with open(os.path.join(dummy_dir, "metadata.txt"), "w", encoding="utf-8") as fh:
            fh.write("[general]\nname=cov_dummy\nversion=0.1\nauthor=coverage\n"
                     "email=cov@example.com\ndescription=dummy plugin for coverage\n"
                     "qgisMinimumVersion=3.0\n")
        with open(os.path.join(dummy_dir, "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write("class DummyPlugin:\n"
                     "    def __init__(self, iface):\n        self.iface = iface\n"
                     "    def initGui(self):\n        pass\n"
                     "    def unload(self):\n        pass\n\n"
                     "def classFactory(iface):\n    return DummyPlugin(iface)\n")
        plugins_root = os.path.dirname(dummy_dir)
        if plugins_root not in sys.path:
            sys.path.insert(0, plugins_root)
        try:
            qu.updateAvailablePlugins()
        except Exception:  # noqa: BLE001
            pass
        try:
            qu.loadPlugin("cov_dummy")
        except Exception:  # noqa: BLE001
            pass
        report.check("plugins.list", lambda: C.call("plugins.list"))
        report.check("plugins.info", lambda: C.call(
            "plugins.info", {"name": "piterm_qgis_ai"}))
        report.check("plugins.enable", lambda: C.call(
            "plugins.enable", {"name": "cov_dummy"}), soft=True)
        report.check("plugins.disable", lambda: C.call(
            "plugins.disable", {"name": "cov_dummy"}), soft=True)
        report.check("plugins.reload", lambda: C.call(
            "plugins.reload", {"name": "cov_dummy"}), soft=True)
        try:
            qu.stopPlugin("cov_dummy")
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(dummy_dir, ignore_errors=True)

    # -------------------------------------------------------------- system
    report.check("system.config_get", lambda: C.call("system.config_get"))
    report.check("system.config_set", lambda: C.call("system.config_set", {
        "key": "limits.max_clients", "value": 16}))
    report.check("system.client_setup", lambda: _client_setup(C))
    report.check("system.audit_tail", lambda: C.call(
        "system.audit_tail", {"limit": 5}), soft=True)
    report.check("system.messages", lambda: C.call(
        "system.messages", {"limit": 10}), soft=True)
    report.check("system.events_poll", lambda: C.call(
        "system.events_poll", {"since": 0, "limit": 20}))
    kicker = BridgeClient("127.0.0.1", FRAME_PORT, client_name="kickme")
    try:
        kicker.call("system.ping")
        clients = report.check("system.clients", lambda: C.call("system.clients"))
        target_id = None
        if isinstance(clients, dict):
            for entry in clients.get("clients", []):
                if entry.get("client") == "kickme" or entry.get("name") == "kickme":
                    target_id = entry.get("id")
        if target_id is None and isinstance(clients, dict) and clients.get("clients"):
            target_id = clients["clients"][-1].get("id")
        if target_id:
            report.check("system.clients_kick", lambda: C.call(
                "system.clients_kick", {"id": target_id}), soft=True)
    finally:
        try:
            kicker.close()
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- finale
    report.check("system.shutdown", lambda: C.call("system.shutdown"))


if __name__ == "__main__":
    sys.exit(main())
