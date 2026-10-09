# Changelog

## Fixed — cross-platform CI (2026-10-09)

* **Linux unit failure fixed.** The first push turned CI red on `ubuntu-latest`
  (both Python versions) while Windows stayed green. Cause: `_norm_path()` in
  `scripts/qgis_command_coverage.py` built its key with
  `os.path.normcase(os.path.normpath(...))`, but on POSIX `normcase` is an
  identity function (no case folding) and `normpath` treats `\` as an ordinary
  character — so two spellings of the same sandbox path compared *unequal* on
  Linux. It is now a pure, platform-independent canonicaliser (folds both
  separators, resolves `.` / `..`, lower-cases everywhere); it is only ever used
  as a comparison key, never to build a real path.

## Unreleased — published to GitHub (2026-10-09)

* Repository pushed to <https://github.com/jhh562yy/piterm> (branch `main`).
* Added GitHub Actions CI (`.github/workflows/ci.yml`): unit suite on
  Ubuntu + Windows × Python 3.9/3.12, the CLI surface, and
  `scripts/ci_checks.py` (catalog shape + required docs).
* Added a root `SECURITY.md` (short policy + private-disclosure route, pointing at
  the full `docs/SECURITY.md`), README badges and a docs navigation line; the
  quick-start section is no longer machine-specific.

## Unreleased — operating manual + client-setup fixes (2026-10-09)

* **`docs/USAGE.md` + `docs/USAGE.zh-CN.md`** (new): the full operating manual —
  install → start → connect (MCP stdio / HTTP / raw TCP / CLI) → call, with runnable
  recipes, the security switch table, the error-code cheat sheet, verification and
  troubleshooting. README and INSTALL now point at it.
* **New command `system.client_setup`** (195 total): ask the bridge for ready-to-paste
  MCP client configs and the resolved client entry point, from a shell or from an
  agent (`{"client": "cursor"}` returns one). Exercised in the headless sweep.
* **Defect #19 — Client-setup handed clients a dead path.** `ui/configurator.py`
  generated `…/mcp/piterm_qgis_mcp/min.py` (the file is `minimal.py`) and derived the
  repo root by counting `dirname` levels, which resolved to `<profile>/python` once the
  plugin was installed. Resolution now lives in the QGIS-free
  `plugin/piterm_qgis_ai/client_paths.py` (configured root → walk up → bundled copy),
  `scripts/install.ps1` bundles `minimal.py` next to the plugin and records
  `meta.repo_root` in the profile ini, and the dialog shows the resolution status
  (red when nothing is found) instead of silently emitting a bad path.
* **Defect #20 — policy switches could not be turned off.** QgsSettings round-trips
  Python `False` as the *string* `"false"`, and `bool("false")` is `True`; a stored
  `mode.allow_execute=false` therefore read back as enabled (same for `read_only` /
  `confirm_destructive`). All settings I/O now goes through `security.coerce_value`
  (single source of truth for reload, `system.config_get`, `system.config_set`), and
  `system.config_set` rejects non-boolean text instead of guessing.
* Tests: **36 → 79** unit tests (`tests/test_client_paths.py`,
  `tests/test_settings_coercion.py`) pinning both defects; `install.ps1`'s ini upsert
  exercised by a scratch harness; matrix re-run green with the new command
  (178 passed / 4 soft, exercised 134/195 headless; live 33/33 per instance).

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
