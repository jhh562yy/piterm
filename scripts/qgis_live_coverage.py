"""Live-GUI coverage sweep - drives a *running* bridge instance.

Covers the command families that need a real GUI session: canvas
(center/extent/render/rotation/scale/zoom/screenshot), selection + identify,
``ui.message`` / attribute table, map themes + bookmarks, plugin listing and
cleanup - 33 checks. Everything else is covered by the headless suite
(``qgis_command_coverage.py``); smoke (``qgis_headless_smoke.py``) is the
63-check baseline.

Usage::

    python scripts/qgis_live_coverage.py 9877
    python scripts/qgis_live_coverage.py 9887 verification/logs/live_9887.json

The output JSON (default ``verification/logs/live_cov_<port>_<stamp>.json``)
records every check with its detail; renders land in
``verification/live_cov_<port>.png``. Exit code 0 only when every hard check
passed, 2 when the bridge is unreachable.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

from piterm_qgis_mcp.minimal import BridgeClient, BridgeError  # noqa: E402

if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
    print("usage: python scripts/qgis_live_coverage.py [port] [out.json]")
    sys.exit(0)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9877
STAMP = time.strftime("%Y%m%d-%H%M%S")
OUT = (sys.argv[2] if len(sys.argv) > 2 else
       os.path.join(ROOT, "verification", "logs",
                    "live_cov_%s_%s.json" % (PORT, STAMP)))
SHOT = os.path.join(tempfile.gettempdir(), "live_cov_%s.png" % PORT)
PNG = os.path.join(ROOT, "verification", "live_cov_%s.png" % PORT)

C = BridgeClient(host="127.0.0.1", port=PORT, timeout=60)

results = []


def rec(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print("[live:%s] %-32s %s %s" % (PORT, name, "ok" if ok else "FAIL",
                                     "" if ok else str(detail)[:160]),
          flush=True)


def call(name, cmd, params=None, soft=False):
    try:
        value = C.call(cmd, params or {})
        rec(name, True)
        return value
    except BridgeError as exc:
        rec(name, soft, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        rec(name, soft, exc)
        return None


# fail fast (and clearly) when the bridge is not running
try:
    C.call("system.ping")
except BridgeError as exc:
    print("LIVE COVERAGE unreachable: no bridge on 127.0.0.1:%s (%s)"
          % (PORT, exc))
    sys.exit(2)

try:
    rec("ping", True)

    call("fixture.add_memory", "layers.add_memory", {
        "name": "live_cov", "geometry_type": "point", "crs": "EPSG:4326",
        "fields": [{"name": "name", "type": "string"}]})
    call("fixture.insert", "features.insert", {
        "layer": "live_cov",
        "features": [
            {"attributes": {"name": "a"},
             "geometry": {"wkt": "POINT (116.0 39.9)"}},
            {"attributes": {"name": "b"},
             "geometry": {"wkt": "POINT (116.1 39.95)"}},
            {"attributes": {"name": "c"},
             "geometry": {"wkt": "POINT (116.2 40.0)"}}]})

    call("canvas.center_set", "canvas.center_set",
         {"x": 116.1, "y": 39.95, "scale": 200000})
    call("canvas.extent_get", "canvas.extent_get")
    call("canvas.extent_set", "canvas.extent_set",
         {"bbox": [115.9, 39.8, 116.3, 40.1], "crs": "EPSG:4326"})
    call("canvas.refresh", "canvas.refresh")
    call("canvas.render", "canvas.render",
         {"path": PNG, "width": 1200, "height": 800})
    call("canvas.render_3d", "canvas.render_3d", {}, soft=True)
    call("canvas.rotation_set", "canvas.rotation_set", {"degrees": 0})
    call("canvas.scale_get", "canvas.scale_get")
    call("canvas.scale_set", "canvas.scale_set", {"scale": 150000})
    call("canvas.zoom_full", "canvas.zoom_full")
    call("layers.zoom_to", "layers.zoom_to", {"layer": "live_cov"})
    call("canvas.zoom_features", "canvas.zoom_features",
         {"layer": "live_cov", "fids": [1, 2]})
    call("canvas.screenshot", "canvas.screenshot", {"path": SHOT})

    call("features.select", "features.select",
         {"layer": "live_cov", "filter": "\"name\" = 'a'"})
    call("features.zoom_selection", "features.zoom_selection",
         {"layer": "live_cov"})
    call("features.selection_clear", "features.selection_clear",
         {"layer": "live_cov"})
    call("features.identify", "features.identify",
         {"x": 116.1, "y": 39.95, "layers": ["live_cov"], "crs": "EPSG:4326"})

    call("ui.message", "ui.message",
         {"text": "Piterm live coverage check", "level": "info",
          "duration": 2})

    call("plugins.list", "plugins.list")
    call("plugins.info", "plugins.info", {"name": "piterm_qgis_ai"})

    call("project.themes_add", "project.themes_add", {"name": "live_theme"})
    call("project.themes_list", "project.themes_list")
    call("project.themes_apply", "project.themes_apply", {"name": "live_theme"})
    call("project.themes_remove", "project.themes_remove", {"name": "live_theme"})

    bm = call("project.bookmarks_add", "project.bookmarks_add",
              {"name": "live_bm", "bbox": [115.9, 39.8, 116.3, 40.1]})
    call("project.bookmarks_list", "project.bookmarks_list")
    if bm and bm.get("id"):
        call("project.bookmarks_zoom", "project.bookmarks_zoom",
             {"id": bm["id"]})
        call("project.bookmarks_remove", "project.bookmarks_remove",
             {"id": bm["id"]})

    # the attribute table window shows up briefly; removing its layer in the
    # next step closes it again
    call("ui.open_attribute_table", "ui.open_attribute_table",
         {"layer": "live_cov"})
    call("cleanup.remove", "layers.remove", {"layer": "live_cov"})
finally:
    C.close()

ok_n = sum(1 for r in results if r["ok"])
failed = len(results) - ok_n
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump({"port": PORT, "ok": failed == 0, "results": results}, fh,
              ensure_ascii=False, indent=1)
print("LIVE COVERAGE port=%s passed=%d failed=%d | %s"
      % (PORT, ok_n, failed, OUT))
sys.exit(0 if failed == 0 else 1)
