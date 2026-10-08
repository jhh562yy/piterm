# Performance notes

## 1. Where time actually goes

| Path | Cost driver | What this bridge does about it |
|---|---|---|
| Transport | copies + syscalls | frame = one `pack` + two `sendall` (header, body; no concat copy). `TCP_NODELAY` on every socket, so small commands don't wait 40 ms for Nagle. |
| Serialization | `json.dumps` of large results | responses over 128 KiB gzip-compress when the client opts in (`meta.gzip`); NDJSON mode exists so shell users pay zero framing overhead. |
| Query | provider scan | `QgsFeatureRequest` with expression push-down, `bbox` prefilter, `setSubsetOfAttributes([...])`, `NoGeometry` flag when geometry isn't needed, pagination (`limit`/`offset`), and **cursors** (`features.cursor_open` + `cursor.fetch`) so one frame never carries 100 k features. |
| Edits | per-feature provider calls | writes go through an **edit buffer** (`startEditing` + macros) and commit once; `features.insert` adds N features in one `addFeatures` call. |
| Rendering | QGIS raster pipe | `canvas.render` renders on `QgsMapRendererParallelJob` with the canvas' own `mapSettings` clone (correct *and* fast: no rebuild of layer stack), simplified geometry + antialiasing flags inherited from the canvas. |
| Algorithms | provider implementation | sync runs pump the Qt event loop every ~50 ms so QGIS stays alive; anything long should use `processing.start` (QgsTask in QGIS's own thread pool — real parallelism). |
| UI | dock repaints | dock metrics refresh at most every 4 s, log capped at 3000 blocks; the bridge never depends on the dock being open. |

## 2. Bounded resources (no unbounded queues anywhere)

| Resource | Bound |
|---|---|
| inbound message | 64 MiB (`MAX_MESSAGE_SIZE`) |
| per-connection outbound backlog | 256 MiB, then the connection is dropped |
| concurrent clients | 16 (`MAX_CLIENTS`) |
| cursors | 32 open, 15 min TTL |
| jobs | 200 retained, finished pruned |
| journal | 5000 entries |
| checkpoints | 31 (LRU) |
| audit log | ring 2000 + 8 MiB file rotation |
| dock log | 3000 blocks |

## 3. Latency budget

* Loopback round-trip for a trivial command (ping) is dominated by Qt event
  handling; `meta.t_ms` is emitted per command and aggregated by
  `system.metrics`, so you can measure your own machine instead of trusting a
  number in a README.
* Plugin-side sync timeout is **55 s**, deliberately below the common 60 s client
  default: QGIS gives up first and reports *why*, instead of the client walking
  away from work that is still running (and possibly still writing).
* `python.exec` enforces its own wall-clock deadline (30 s default) with a trace
  hook, so an accidental `while True:` in agent code cannot wedge QGIS.

## 4. Agent-side usage checklist (biggest wins)

1. Prefer `bbox` + `filter` + `limit`; never "query everything then filter in
   the model".
2. Ask for `geometry="none"` unless you need geometry; ask for `wkb_base64` only
   when you must move exact coordinates.
3. For >2 k features, open a cursor and fetch pages (`n=500`).
4. Batch independent calls with `system.batch` (one round-trip, per-item errors).
5. Use `processing.start` for anything that can exceed ~5 s; poll `job.get` or
   subscribe to `job` events.
6. Render screenshots at the size you need (`canvas.render width/height/dpi`);
   2000 px default cap keeps frames small.
7. Reuse one connection (the MCP server and CLI both pool connections; raw
   sockets should too — the protocol is connection-oriented by design).

## 5. Things that were deliberately *not* done

* **No worker threads inside QGIS** (PyQGIS is not thread-safe): parallel work
  goes through QGIS's own task manager, where QGIS manages thread affinity.
* **No Arrow/zero-copy geometry channel**: loopback bandwidth is not the
  bottleneck, correctness is. WKT/WKB options cover the use cases.
* **No compression on by default**: gzip on loopback costs CPU for little gain;
  it's opt-in per client (`meta.gzip`) and automatic for big HTTP payloads when
  requested.
* **No polling loops in the plugin**: all I/O is signal-driven; the only timer
  is a 5 s heartbeat that expires cursors and reaps dead sockets.

## 6. Measured on this machine (live GUI, QGIS 3.44.15, 2026-10-08)

Loopback TCP, single client, Python `BridgeClient` (method script:
`_scratch/bench.py`, 1 000-feature grid, HTTP/TCP defaults):

| Operation | Result |
|---|---|
| `system.ping` ×300 | **0.96 ms** avg round-trip |
| `layers.add_memory` (new layer) | 214 ms one-off |
| `features.insert` ×1000 in one call | **44.8 ms** (~22 k features/s) |
| `layers.count` on 1 000 features | 23.8 ms |
| `features.query` limit 500 | 16.8 ms |
| `layers.zoom_to` | 3.1 ms |
| `canvas.render` 1600×1000 | 223 ms |
| `system.batch` ×50 pings | 11.5 ms (0.23 ms/item) |

Render artifact: `verification/bench_render.png`.
