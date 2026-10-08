#!/usr/bin/env python3
"""Batch + workflow demo: one round-trip, references, dry-run, jobs.

    python examples/workflow_batch.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

from piterm_qgis_mcp.minimal import BridgeClient, BridgeError  # noqa: E402


def main():
    client = BridgeClient(client_name="workflow-demo")
    try:
        # A workflow: buffer a layer, then count the result.
        # "$0.result.layer.id" points at the layer added by step 0.
        workflow = {
            "steps": [
                {"type": "layers.add_memory", "params": {
                    "name": "wf_roads", "geometry_type": "line",
                    "crs": "EPSG:3857"}},
                {"type": "features.insert", "params": {
                    "layer": "wf_roads",
                    "features": [
                        {"geometry": {"wkt": "LINESTRING (0 0, 100 0)"}},
                        {"geometry": {"wkt": "LINESTRING (100 0, 100 100)"}},
                    ]}},
                {"type": "processing.execute", "params": {
                    "algorithm": "native:buffer",
                    "parameters": {"INPUT": "$0.result.layer.id",
                                   "DISTANCE": 10, "OUTPUT": "memory:wf_buf"},
                    "load_results": True}},
                {"type": "layers.count", "params": {
                    "layer": "$2.result.loaded_layers.0.id"}},
            ],
            "stop_on_error": True,
        }

        print("dry run:", client.call("session.workflow_run",
                                      {**workflow, "dry_run": True})["ok_count"])
        result = client.call("session.workflow_run", workflow)
        print("workflow ok_count:", result["ok_count"])
        print("step 3 result:", result["results"][-1]["result"])

        # Long job: background buffer with progress polling.
        job = client.call("processing.start", {
            "algorithm": "native:buffer",
            "parameters": {"INPUT": "wf_roads", "DISTANCE": 25,
                           "OUTPUT": "memory:wf_buf_async"},
            "load_results": True})
        for _ in range(60):
            state = client.call("job.get", {"id": job["job"]})
            if state["state"] in ("succeeded", "failed", "cancelled"):
                print("job:", state["state"], state.get("progress"))
                break
            time.sleep(0.5)

        # Batch: many reads, one round-trip.
        batch = client.call("system.batch", {"commands": [
            {"type": "layers.list"},
            {"type": "job.list"},
            {"type": "system.metrics"},
        ]})
        print("batch ok_count:", batch["ok_count"])
    except BridgeError as exc:
        print("bridge error:", exc.to_text(), file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
