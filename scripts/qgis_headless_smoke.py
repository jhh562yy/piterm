"""Headless end-to-end smoke test - run inside QGIS's Python.

    "C:\\Program Files\\QGIS 3.44.15\\bin\\python-qgis-ltr.bat" scripts/qgis_headless_smoke.py

Boots the bridge server, then runs the Qt event loop on the main thread while a
worker thread drives the bridge over TCP frames + HTTP (mirroring how a real
client behaves; without a running event loop the QTcpServer could never accept).
Writes a JSON report to ``verification/logs/`` and exits non-zero on any failed
check.
"""

from __future__ import annotations

import faulthandler
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

faulthandler.enable()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAME_PORT = 19877
HTTP_PORT = 19879


class Report:
    def __init__(self):
        self.ok = True
        self.checks = []

    def check(self, name, fn, expect_error=None):
        started = time.perf_counter()
        try:
            value = fn()
            if expect_error:
                raise AssertionError("expected error %s, got %r"
                                     % (expect_error, _short(value)))
            self.checks.append({"name": name, "ok": True,
                                "ms": round((time.perf_counter() - started) * 1000, 1),
                                "value": _short(value)})
            print("[check] %-38s ok (%.0f ms)" % (
                name, (time.perf_counter() - started) * 1000), flush=True)
            return value
        except Exception as exc:  # noqa: BLE001
            if expect_error and getattr(exc, "code", None) == expect_error:
                self.checks.append({"name": name, "ok": True,
                                    "expected_error": expect_error,
                                    "ms": round((time.perf_counter() - started) * 1000, 1)})
                print("[check] %-38s ok (expected %s)" % (name, expect_error),
                      flush=True)
                return None
            self.ok = False
            self.checks.append({"name": name, "ok": False,
                                "ms": round((time.perf_counter() - started) * 1000, 1),
                                "error": "%s: %s" % (type(exc).__name__, exc)})
            print("[check] %-38s FAIL: %s: %s" % (name, type(exc).__name__, exc),
                  flush=True)
            return None


def main():
    sys.path.insert(0, os.path.join(ROOT, "plugin"))
    sys.path.insert(0, os.path.join(ROOT, "mcp"))

    from qgis.core import Qgis, QgsApplication
    from qgis.PyQt.QtCore import QTimer

    app_name = ("QGIS4-piterm-smoke" if Qgis.QGIS_VERSION_INT >= 40000
                else "QGIS3-piterm-smoke")
    QgsApplication.setOrganizationName("QGIS")
    QgsApplication.setOrganizationDomain("qgis.org")
    QgsApplication.setApplicationName(app_name)
    app = QgsApplication([], False)
    app.setApplicationName(app_name)
    app.initQgis()
    report = Report()
    report_data = {"ok": True, "qgis": Qgis.QGIS_VERSION, "checks": []}
    tmp = tempfile.mkdtemp(prefix="piterm_smoke_")
    state = {"timeout": False}

    server = None
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

        from piterm_qgis_mcp.minimal import BridgeClient, BridgeError

        client = BridgeClient("127.0.0.1", FRAME_PORT, client_name="smoke")
        client_long = BridgeClient("127.0.0.1", FRAME_PORT, client_name="smoke-long",
                                   timeout=120)

        def run_checks():
            try:
                checks = build_checks(report, client, client_long, server,
                                      settings, tmp, app)
                checks()
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
        safety.setInterval(150000)
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
    if state["timeout"]:
        report_data["fatal"] = "safety timeout: the run never completed"
    logs_dir = os.path.join(ROOT, "verification", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(logs_dir, "smoke_%s_%s.json"
                       % (report_data["qgis"].replace(" ", "_"), stamp))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report_data, fh, ensure_ascii=False, indent=1)
    passed = sum(1 for c in report_data["checks"] if c.get("ok"))
    failed = sum(1 for c in report_data["checks"] if not c.get("ok"))
    print("SMOKE %s | passed=%d failed=%d | %s"
          % ("OK" if report_data["ok"] else "FAIL", passed, failed, out))
    if report_data.get("fatal"):
        print("FATAL: %s" % report_data["fatal"].splitlines()[0])
    for entry in report_data["checks"]:
        if not entry.get("ok"):
            print("  FAIL %s: %s" % (entry["name"], entry.get("error")))
    return 0 if report_data["ok"] else 1


def build_checks(report, client, client_long, server, settings, tmp, app):
    """Return a callable running every check (executed on the worker thread)."""

    def run():
        from piterm_qgis_mcp.minimal import BridgeClient

        # ---- connectivity / discovery --------------------------------
        report.check("ping", lambda: client.call("system.ping"))
        hello = report.check("hello", lambda: client.call("system.hello"))
        if hello:
            report.check("command_count>=120",
                         lambda: _assert(hello["commands"]["count"] >= 120,
                                         "only %s commands" % hello["commands"]["count"]))
        report.check("info", lambda: client.call("system.info"))
        report.check("help.search", lambda: client.call(
            "system.help", {"query": "layers", "limit": 10}))
        report.check("capabilities", lambda: len(client.call(
            "system.capabilities")["commands"]))
        report.check("metrics", lambda: client.call("system.metrics"))
        report.check("diagnose", lambda: client.call("system.diagnose"))

        # ---- HTTP transport -------------------------------------------
        def http_ping():
            body = json.dumps({"type": "system.ping"}).encode("utf-8")
            request = urllib.request.Request(
                "http://127.0.0.1:%d/rpc" % HTTP_PORT, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload["result"]

        report.check("http.ping", http_ping)

        # ---- layers ---------------------------------------------------
        report.check("layers.add_memory", lambda: client.call(
            "layers.add_memory", {
                "name": "smoke_points", "geometry_type": "point",
                "crs": "EPSG:4326",
                "fields": [{"name": "name", "type": "string"},
                           {"name": "value", "type": "double"}]}))
        report.check("layers.list", lambda: client.call("layers.list"))
        report.check("layers.info", lambda: client.call(
            "layers.info", {"layer": "smoke_points"}))
        report.check("layers.fields_add", lambda: client.call(
            "layers.fields_add", {"layer": "smoke_points", "name": "extra",
                                  "type": "int"}))
        report.check("layers.tree", lambda: client.call("layers.tree"))

        # ---- features -------------------------------------------------
        report.check("features.insert", lambda: client.call("features.insert", {
            "layer": "smoke_points",
            "features": [
                {"attributes": {"name": "a", "value": 1.5},
                 "geometry": {"wkt": "POINT (1 1)"}},
                {"attributes": {"name": "b", "value": 3.0},
                 "geometry": {"wkt": "POINT (2 2)"}},
                {"attributes": {"name": "c", "value": 4.5},
                 "geometry": {"wkt": "POINT (3 3)"}},
            ]}))
        query = report.check("features.query", lambda: client.call(
            "features.query", {"layer": "smoke_points", "limit": 10,
                               "order_by": "value desc"}))
        if query:
            report.check("features.query.ordered",
                         lambda: _assert(
                             query["features"][0]["attributes"]["value"] == 4.5,
                             "ordering wrong: %r" % query["features"][0]))
        report.check("features.count", lambda: client.call(
            "layers.count", {"layer": "smoke_points"}))
        report.check("features.field_stats", lambda: client.call(
            "features.field_stats", {"layer": "smoke_points",
                                     "field": "value"}))
        report.check("features.unique", lambda: client.call(
            "layers.unique_values", {"layer": "smoke_points", "field": "name"}))
        report.check("features.update", lambda: client.call("features.update", {
            "layer": "smoke_points",
            "updates": [{"fid": 1, "attributes": {"name": "a2"}}]}))
        report.check("features.select", lambda: client.call("features.select", {
            "layer": "smoke_points", "filter": "\"value\" > 2"}))
        report.check("features.selection", lambda: client.call(
            "features.selection_get", {"layer": "smoke_points"}))
        report.check("field_calculator", lambda: client.call(
            "features.field_calculator", {
                "layer": "smoke_points", "new_field": "doubled",
                "expression": "\"value\" * 2"}))
        report.check("geometry_info", lambda: client.call(
            "features.geometry_info", {"layer": "smoke_points", "fid": 1}))
        cursor = report.check("cursor.open", lambda: client.call(
            "features.cursor_open", {"layer": "smoke_points"}))
        if cursor:
            report.check("cursor.fetch", lambda: client.call(
                "features.cursor_fetch", {"cursor": cursor["cursor"], "n": 2}))
            report.check("cursor.close", lambda: client.call(
                "features.cursor_close", {"cursor": cursor["cursor"]}))

        # ---- expressions ----------------------------------------------
        report.check("expression.validate", lambda: client.call(
            "expression.validate", {"expression": "\"value\" * 2",
                                    "layer": "smoke_points"}))
        report.check("expression.evaluate", lambda: client.call(
            "expression.evaluate", {"expression": "cos(0) + 1"}))
        report.check("transform.coordinates", lambda: client.call(
            "features.transform_coordinates",
            {"from_crs": "EPSG:4326", "to_crs": "EPSG:3857",
             "points": [[0, 0], [1, 1]]}))

        # ---- style (headless-safe parts) ------------------------------
        report.check("style.renderer_set.graduated", lambda: client.call(
            "style.renderer_set", {"layer": "smoke_points", "mode": "graduated",
                                   "field": "value", "classes": 3}))
        report.check("style.renderer_get", lambda: client.call(
            "style.renderer_get", {"layer": "smoke_points"}))
        report.check("style.ramps_list", lambda: client.call(
            "style.ramps_list", {"limit": 5}))

        # ---- processing -----------------------------------------------
        report.check("processing.providers", lambda: client.call(
            "processing.providers"))
        report.check("processing.search", lambda: client.call(
            "processing.algorithms_search", {"query": "buffer", "limit": 5}))
        report.check("processing.execute.buffer", lambda: client_long.call(
            "processing.execute", {
                "algorithm": "native:buffer",
                "parameters": {"INPUT": "smoke_points", "DISTANCE": 1.0,
                               "OUTPUT": "memory:smoke_buf"},
                "load_results": True}))
        report.check("processing.start.job", lambda: client.call(
            "processing.start", {
                "algorithm": "native:buffer",
                "parameters": {"INPUT": "smoke_points", "DISTANCE": 2.0,
                               "OUTPUT": "memory:smoke_buf2"}}))
        deadline = time.time() + 40
        job_ok = {"done": False, "state": None}
        while time.time() < deadline:
            listing = client.call("job.list", {"limit": 5})["jobs"]
            states = [j["state"] for j in listing if j["kind"] == "processing"]
            if states:
                job_ok["state"] = states[0]
                if states[0] in ("succeeded", "failed", "cancelled"):
                    job_ok["done"] = True
                    break
            time.sleep(0.3)
        report.check("job.completed",
                     lambda: _assert(job_ok["done"],
                                     "job never finished (state=%s)" % job_ok["state"]))
        report.check("job.list", lambda: client.call("job.list"))

        # ---- io / project / session -----------------------------------
        report.check("io.export_layer", lambda: client.call("io.export_layer", {
            "layer": "smoke_points", "path": os.path.join(tmp, "points.geojson")}))
        report.check("io.export.exists", lambda: _assert(
            os.path.exists(os.path.join(tmp, "points.geojson")),
            "geojson missing"))
        report.check("io.read_text", lambda: client.call(
            "io.read_text", {"path": os.path.join(tmp, "points.geojson"),
                             "max_bytes": 400}))
        project_path = os.path.join(tmp, "smoke.qgz")
        report.check("project.save", lambda: client.call(
            "project.save", {"path": project_path}))
        report.check("project.info", lambda: client.call("project.info"))
        report.check("project.variables", lambda: client.call(
            "project.variables_set", {"key": "stage", "value": "smoke"}))
        report.check("checkpoint.create", lambda: client.call(
            "session.checkpoint_create", {"name": "smoke"}))
        report.check("checkpoint.list", lambda: client.call(
            "session.checkpoint_list"))
        replay = os.path.join(tmp, "replay.py")
        report.check("journal.export", lambda: client.call(
            "session.journal_export", {"path": replay}))
        report.check("journal.replay.exists", lambda: _assert(
            os.path.exists(replay), "replay script missing"))
        report.check("workflow.run", lambda: client.call("session.workflow_run", {
            "steps": [
                {"type": "layers.count", "params": {"layer": "smoke_points"}},
                {"type": "features.query",
                 "params": {"layer": "smoke_points", "limit": 1}},
                {"type": "project.info", "params": {}},
            ], "stop_on_error": True}))
        report.check("workflow.refs", lambda: client.call("session.workflow_run", {
            "steps": [
                {"type": "layers.list", "params": {}},
                {"type": "layers.count",
                 "params": {"layer": "$0.result.layers.0.id"}},
            ]}))
        report.check("workflow.dry_run", lambda: client.call(
            "session.workflow_run", {
                "steps": [{"type": "layers.remove",
                           "params": {"layer": "smoke_points"}}],
                "dry_run": True}))
        report.check("batch", lambda: client.call("system.batch", {"commands": [
            {"type": "system.ping"},
            {"type": "layers.count", "params": {"layer": "smoke_points"}},
        ]}))
        report.check("undo.status", lambda: client.call(
            "session.edit_status", {"layer": "smoke_points"}))

        # ---- policy: read-only ----------------------------------------
        settings.set("mode.read_only", True)
        server.security.refresh()
        report.check("policy.read_only_blocks_write",
                     lambda: client.call("features.insert", {
                         "layer": "smoke_points",
                         "features": [{"attributes": {"name": "x"},
                                       "geometry": {"wkt": "POINT (9 9)"}}]}),
                     expect_error="E_FORBIDDEN")
        report.check("policy.read_only_allows_read",
                     lambda: client.call("layers.count",
                                         {"layer": "smoke_points"}))
        settings.set("mode.read_only", False)
        server.security.refresh()

        # ---- policy: token --------------------------------------------
        settings.set("auth.enabled", True)
        settings.set("auth.token", "smoke-secret")
        server.security.refresh()
        bad = BridgeClient("127.0.0.1", FRAME_PORT, token="wrong")
        report.check("policy.token_rejected", lambda: bad.call("system.ping"),
                     expect_error="E_AUTH")
        bad.close()
        good = BridgeClient("127.0.0.1", FRAME_PORT, token="smoke-secret")
        report.check("policy.token_ok", lambda: good.call("system.ping"))
        good.close()
        settings.set("auth.enabled", False)
        server.security.refresh()

        # ---- policy: paths --------------------------------------------
        settings.set("paths.allow", [tmp])
        server.security.refresh()
        report.check("policy.path_denied", lambda: client.call(
            "io.read_text", {"path": "C:\\Windows\\win.ini"}),
            expect_error="E_PATH_DENIED")

        # ---- registry self-checks -------------------------------------
        from piterm_qgis_ai.registry import command_count
        report.check("registry.count>=120",
                     lambda: _assert(command_count() >= 120,
                                     "registry has %s commands" % command_count()))
        report.check("registry.count.note", lambda: command_count())

    return run


def _assert(value, message="assertion failed"):
    if not value:
        raise AssertionError(message)
    return value


def _short(value, limit=400):
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text[:limit]


if __name__ == "__main__":
    sys.exit(main())
