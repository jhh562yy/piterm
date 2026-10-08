# Changelog

## Unreleased — command-coverage sweep (2026-10-09)

* New `scripts/qgis_command_coverage.py` (headless) + live-GUI sweep: the union
  of smoke + coverage exercises **192 of 194 commands** at that point (the two
  db-writers were skipped by design — lifted to 194/194 below); 149/0 headless
  on 3.44.15 and 4.2.3, 33/33 live on both GUIs.
* 17 defects across 21 commands fixed (layer-tree reorder/group moves no longer
  delete layers; delimited-text URI; multi-frame layout tables; bookmark
  add/zoom API changes; map-theme 3.44 signatures; `setDataSource`/`setScale`/
  `rollBack`/`colorRampType` binding fallbacks; deferred `system.shutdown`
  reply; plugin enable/disable fallbacks; zero-area selection zoom).
* Sandbox completion: `scripts/qgis_command_coverage.py --sandbox-db`
  exercises `db.connection_add` / `db.import_layer` for real against a
  redirected settings root (`QGIS_CUSTOM_CONFIG_PATH` inside
  `_scratch/cov_sandbox`, run-time interlock, operator-profile hash diff) — the
  union now reaches **194/194** on 3.44.15, 4.2.3 and 4.3.0-dev (177 passed /
  0 hard failures each, `skipped_by_design` empty).
* Defect #18: `db.import_layer` was broken on all verified builds (removed
  `createVectorLayer` / missing `VectorLayerOptions`) — file kinds now use
  `QgsVectorFileWriter.CreateOrOverwriteLayer` (siblings preserved; overwrite
  verified 5→2 rows), server kinds keep an exporter fallback.
* Coverage-script hygiene: QGIS-native GeoPackage fixture, per-run port
  overrides (`PITERM_COV_PORT`…), failure `detail` in logs, zero
  ResourceWarnings under `-X dev`.
* Verification suites are fully in-repo and one-command now: the live-GUI sweep
  moved to `scripts/qgis_live_coverage.py` (portable paths, per-check JSON,
  non-zero exit on failure), and `scripts/verify_all.ps1` runs pytest + smoke +
  sandboxed coverage + live in one pass (full run log under
  `verification/logs/verify_all_*.log`; the whole reference matrix takes ≈36 s).
* New `tests/test_coverage_sandbox.py` pins the `--sandbox-db` interlock
  (settings root must live under `_scratch/cov_sandbox`; prefix tricks and
  mismatched env roots are rejected) and the cleanup helpers — test suite
  **26 → 36**.
* README refreshed: 160+ → **194 commands**; the testing section documents the
  full matrix and `verify_all.ps1`.

## 1.0.0 — 2026-10-08

First complete release. Everything below is implemented, wired and covered by
tests (unit + headless QGIS end-to-end on 3.44.15 and 4.2.3).

### Plugin (`piterm_qgis_ai`)
* Zero-thread bridge server on QGIS's Qt event loop (`QTcpServer`), reentrancy
  guard, per-connection contexts, heartbeat, client cap/kick.
* Three transports: length-prefixed frames (gzip-aware), NDJSON lines, HTTP
  JSON-RPC (`/rpc`, `/health`, `/info`, `/commands`, `/events`, CORS).
* **160+ commands** across system/project/layers/features/raster/processing/
  job/canvas/style/layout/session/io/db/plugins/expression/python/ui; see
  `docs/TOOLS.md` (generated).
* Dispatch pipeline: auth → rate limit → registry → policy → param validation →
  handler → timing → journal/audit, with structured error codes and difflib
  suggestions for typos.
* Security: token (constant-time, lockout), scopes, read-only mode, path
  allow/deny lists, `__confirm` gate, audit JSONL, non-loopback refuse-to-start.
* Session: checkpoints (31, LRU) with guarded restore, journal + replay-script
  export, edit sessions with undo/redo, `workflow_run` with `$N.field`
  references and dry-run.
* Performance: cursor paging, batch round-trips, per-command `t_ms`, gzip for
  large responses, request flags (subset attributes, no-geometry), job system on
  QgsTask, 55 s sync budget with progress pumping.
* UI: dock (status/clients/log/console-with-autocomplete/metrics), live settings,
  client setup dialog with 15+ prebuilt configs, programmatic π icon.
* Compatibility: QGIS 3.28–4.x via one `compat.resolve()` layer; Qt5/Qt6.

### MCP layer (`piterm_qgis_mcp`, MIT)
* `minimal.py`: single-file stdlib client + MCP stdio server + CLI
  (`ping/call/tools/screenshot/batch/mcp-stdio`), image-aware tool results.
* `server.py`: FastMCP server with **dynamic tools from the live catalog**
  (no drift), meta tools, resources (`qgis://info|project|layers|help`), prompts,
  tool annotations (readOnly/destructive), compound mode.
* Tool catalog export (`manifest.json`) + generated `docs/TOOLS.md`.

### Skill & tooling
* `skill/piterm-qgis-ai/` — SKILL.md with recipes + the zero-dependency script.
* `scripts/install.ps1` (multi-profile, backup, ini enable, uninstall),
  `install.py` (cross-platform), `scripts/export_manifest.py`,
  `scripts/qgis_headless_smoke.py` (event-loop-correct end-to-end suite).
* Test suite: wire codec, security policy, registry manifest (AST, ≥120 asserts),
  fake-bridge round-trip + MCP handshake.

### Research
* Source-level study of nkarasiak/qgis-mcp (125 tools), jjsantos01/qgis_mcp
  (15 tools), opengeos/GeoAgent (29 tools) — see `docs/RESEARCH.md`; design
  decisions traced to concrete mechanisms and pitfalls found there.
