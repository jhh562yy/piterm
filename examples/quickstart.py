#!/usr/bin/env python3
"""Quickstart: drive QGIS through the Piterm bridge (stdlib only).

Prerequisite: QGIS is open and the Piterm QGIS AI bridge is started
(dock → Start server). Run:

    python examples/quickstart.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

from piterm_qgis_mcp.minimal import BridgeClient, BridgeError  # noqa: E402


def main():
    client = BridgeClient(client_name="quickstart")
    try:
        print("ping:", client.ping())

        # 1) create a memory layer with a couple of fields
        client.call("layers.add_memory", {
            "name": "quickstart_points",
            "geometry_type": "point",
            "crs": "EPSG:4326",
            "fields": [{"name": "label", "type": "string"},
                       {"name": "weight", "type": "double"}],
        })

        # 2) insert features
        client.call("features.insert", {
            "layer": "quickstart_points",
            "features": [
                {"attributes": {"label": "alpha", "weight": 1.0},
                 "geometry": {"wkt": "POINT (1 1)"}},
                {"attributes": {"label": "beta", "weight": 2.5},
                 "geometry": {"wkt": "POINT (2 2)"}},
            ],
        })

        # 3) query them back
        result = client.call("features.query", {
            "layer": "quickstart_points", "limit": 10,
            "order_by": "weight desc"})
        for feature in result["features"]:
            print("feature:", feature["attributes"], feature["geometry"])

        # 4) zoom + screenshot + render
        client.call("layers.zoom_to", {"layer": "quickstart_points"})
        shot = os.path.join(tempfile.gettempdir(), "piterm_quickstart_canvas.png")
        client.screenshot(shot)
        print("canvas screenshot:", shot)
        render = os.path.join(tempfile.gettempdir(), "piterm_quickstart_render.png")
        client.call("canvas.render", {"width": 1200, "path": render})
        print("render:", render)

        # 5) stats + checkpoint
        stats = client.call("features.field_stats", {
            "layer": "quickstart_points", "field": "weight"})
        print("weight stats:", stats["stats"])
        print("checkpoint:", client.call("session.checkpoint_create",
                                         {"name": "quickstart"}))
    except BridgeError as exc:
        print("bridge error:", exc.to_text(), file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
