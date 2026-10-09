# Verification report — Piterm QGIS AI 1.0.0 (contract CT-20261008-01)

**Date:** 2026-10-08 · **Status:** all AI-side lights green; awaiting user-side
acceptance in the contract module.
**Deliverable:** `G:\AIato\pi\piterm-qgis-ai` — 92 files, ~22 000 lines of
source/docs (excluding `_reference/` research checkouts and `.venv`).

---

## 1. What was built

| Component | Path | Notes |
|---|---|---|
| QGIS plugin | `plugin/piterm_qgis_ai/` | 0-thread bridge on the Qt event loop; **195 commands** (generated catalog), 16 domain handler modules, dock UI, client configurator (`client_paths.py` resolves the client entry in both checkout & installed layouts) |
| MCP server | `mcp/piterm_qgis_mcp/` | `minimal.py` (stdlib-only client + MCP stdio server + CLI, sha16 `A365F2500C8B98AC`), `server.py` (FastMCP for mcp 1.x / MCPServer for mcp 2.x), dynamic tools from the live catalog |
| Skill | `skill/piterm-qgis-ai/` | `SKILL.md` + `piterm_qgis.py`; **installed into this environment** as `skill://piterm-qgis-ai` |
| Installers | `scripts/install.ps1`, `install.py` | multi-profile deploy, backups, ini enable, uninstall |
| Docs | `docs/` | USAGE (+ zh-CN) / RESEARCH / ARCHITECTURE / PROTOCOL / TOOLS (generated, 195 commands) / SECURITY / PERFORMANCE / INSTALL / COMPAT / CHANGELOG |
| Tests | `tests/` | 79 unit tests (wire, security, manifest AST, fake-bridge round-trip, sandbox interlock, client-path resolution, settings coercion) |

Key artifacts: `docs/TOOLS.md` (23 KB), `mcp/piterm_qgis_mcp/manifest.json`
(94 KB, sha16 `DF9909370C3D2C8C`), plugin `server.py` sha16 `5F96CB548E3338F8`.

## 2. Environment

* QGIS 3.44.15 LTR (Qt 5.15.13, Python 3.12.14) — profiles
  `%APPDATA%\QGIS\QGIS3\profiles\default`
* QGIS 4.2.3 (Qt 6.11.0, Python 3.12.14) — `%APPDATA%\QGIS\QGIS4\profiles\default`
* uv 0.11.26; mcp 2.3.0 installed in repo `.venv` (35 packages)
* Reference checkouts: nkarasiak/qgis-mcp (125 tools), jjsantos01/qgis_mcp (15),
  opengeos/GeoAgent (29) — see `docs/RESEARCH.md`

## 3. Acceptance matrix (R1–R7)

| # | Criterion | Evidence | Result |
|---|---|---|---|
| R1 | Source-level benchmark study | `docs/RESEARCH.md` — architecture, mechanisms, pitfalls of all three references + design-decision table | ✅ |
| R2 | Plugin loads headless on QGIS 3.44 **and** 4.2 | `scripts/qgis_headless_smoke.py` → **63/63 checks** on **3.44.15, 4.2.3 and 4.3.0-dev**: `logs/smoke_3.44.15-Solothurn_20261008-234944.json`, `smoke_4.2.3-Belém_do_Pará_20261008-235005.json`, `smoke_4.3.0-Master_20261008-235219.json` | ✅ |
| R3 | MCP server handshake + tool calls | minimal stdio: `tests/test_min_roundtrip.py`; official SDK probe: `scripts/mcp_fastmcp_probe.py` against live QGIS → `initialize ok (protocol 2025-11-25)`, **196 tools**, `system_ping`/`layers_list`/`piterm_status` all answered; streamable-http transport verified end-to-end too (loopback 18990, see `logs/mcp_streamable_http_probe_20261008.md`) | ✅ |
| R4 | Skill/CLI performs real operations | live QGIS 3.44 via CLI: `layers.add_memory`, `features.insert` (5), `layers.zoom_to`, `canvas.render`, `canvas.screenshot`, `processing.execute native:buffer`, `session.checkpoint_create`; HTTP `/rpc` ping; both GUI instances answering (9877 / 9887) | ✅ |
| R5 | Deploy scripts with backup/uninstall | `scripts/install.ps1` deployed to both profiles (39 files each), ini enable + backups (`...plugins\piterm_qgis_ai.bak-20261008-233919`, `QGIS3.ini.bak-piterm-*`), autostart configured for both; **uninstall → reinstall cycle executed live (23:51)** — dirs removed, ini false→true with backups, live bridges unaffected, redeploy 39 files with matching hashes | ✅ |
| R6 | Security & control chain | read-only blocks writes (`E_FORBIDDEN`), token reject/accept (`E_AUTH` / ok), path allow/deny (`E_PATH_DENIED`), audit + journal + checkpoint + undo verified in smoke; sandbox rules unit-tested | ✅ |
| R7 | Docs complete + generated TOOLS ≥120 | `docs/TOOLS.md` generated with **195 commands**; USAGE (+ zh-CN)/README/ARCHITECTURE/PROTOCOL/SECURITY/PERFORMANCE/INSTALL/COMPAT/CHANGELOG/RESEARCH present | ✅ |

## 4. Exact commands (reproducible)

```powershell
cd G:\AIato\pi\piterm-qgis-ai
# unit tests (no QGIS)
$env:PYTHONPATH='G:\AIato\pi\piterm-qgis-ai\_tools\py'; python -m pytest tests -q
# headless end-to-end (both generations)
& 'C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat' -u scripts\qgis_headless_smoke.py
& 'C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat'       -u scripts\qgis_headless_smoke.py
# regenerate the catalog after plugin changes
& 'C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat' -u scripts\export_manifest.py
# full MCP (needs .venv with mcp>=2)
.\.venv\Scripts\python.exe scripts\mcp_fastmcp_probe.py --port 9877
# deploy / uninstall
powershell -ExecutionPolicy Bypass -File scripts\install.ps1          # or -All / -Uninstall
```

## 5. Live-GUI evidence (2026-10-08 23:34–23:42 local)

* QGIS 3.44 (9877/9879) and QGIS 4.2.3 (9887/9889) both running with the
  bridge auto-started; `system.info` confirmed Qt 5.15.13 vs Qt 6.11.0,
  `is_qgis4: true` on the 4.x instance.
* Live operations transcript: 5 points inserted into `live_demo2`, buffer
  algorithm executed **inside the running GUI**, checkpoint
  `ckpt-20261008-233436-1` created.
* Images: `verification/live_map.png` (server-side render, 2100×1350),
  `verification/live_canvas.png` (canvas grab 1584×1245),
  `verification/live_desktop.png` (full desktop with QGIS + dock).
* Audit/journal/backups exist on disk under the two profiles.

**Final-state re-verification (23:49–23:56 local, post-settlement):** fresh
headless smokes re-ran green — **63/63** on 3.44.15, 4.2.3 *and* the 4.3.0-dev
build (`logs/smoke_3.44.15-Solothurn_20261008-234944.json`,
`smoke_4.2.3-Belém_do_Pará_20261008-235005.json`,
`smoke_4.3.0-Master_20261008-235219.json`); MCP **streamable-http** probe passed
on loopback (`logs/mcp_streamable_http_probe_20261008.md`); live E2E redone on
both instances (`final_344.png` / `final_42.png`, layer count back to 0);
**uninstall → reinstall cycle** executed live with backups and matching hashes
(bridges unaffected); **measured performance** added to `docs/PERFORMANCE.md`
(`bench_render.png`); the dock console was **driven in the live GUI** — the
"Run command" button clicked through the Qt widget layer, `layers.list`
answered into the console output (`dock_console_run.png`); skill CLI
`ping/hello` answered.

## 6. Real defects found & fixed during verification

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `canvas.render` failed; heap corruption after | `QgsMapRendererParallelJob.isFinished()` not exposed in PyQt; job left running | wait on the `finished` signal flag + cancel/wait on timeout (`handlers/canvas.py`) |
| 2 | `processing.start` crash `0xc0000374` (heap corruption) | `QgsProcessingAlgRunnerTask` keeps raw pointers to Python-owned `context`/`feedback`; GC during run | strong refs held in `server.qt_tasks` until the task finishes (`server.py`, `handlers/processing.py`) |
| 3 | `style.renderer_set graduated` native access violation | `QgsGraduatedSymbolRenderer.createRenderer` crashes on 3.44 headless | renderer built manually from computed breaks (`handlers/style.py`) |
| 4 | `no algorithm 'native:buffer'` headless | Processing providers never initialized outside the GUI | lazy `Processing.initialize()` + `QgsNativeAlgorithms` fallback (`handlers/processing.py`) |
| 5 | `paths.allow=["*"]` didn't allow anything | wildcard was expanded to an abs path before comparison | wildcard short-circuit (`security.py`) |
| 6 | MCP schema typed everything as string | optional params had no annotations | type inference from defaults (`registry.py`) + regenerate manifest |
| 7 | FastMCP import failed under mcp 2.3 | FastMCP renamed to `MCPServer`; `serverInfo`→`server_info` | import chain + probe field fix (`mcp/server.py`, `scripts/mcp_fastmcp_probe.py`) |

## 7. Known limits / not machine-verified

* QGIS 3.28–3.40 declared supported via the compat layer; executed here:
  3.44.15, 4.2.3 and the 4.3.0-dev build (`D:\qgis\4.3.0`) — all 63/63. The
  `C:\Program Files\QGIS 4.2.0` directory is an incomplete OSGeo4W stub
  (`bin\setup.bat` only, no interpreter), so the 4.2 line is covered by 4.2.3.
* macOS/Linux paths in `install.py` are untested on this machine.
* `streamable-http` transport of the full MCP server: verified end-to-end on
  loopback (127.0.0.1:18990, 2026-10-08 23:49) in addition to the stdio path.
* GUI screenshots were taken from the desktop; the dock's interactive console
  was additionally exercised in the live GUI (2026-10-08 23:56) — the "Run
  command" button was clicked through the Qt widget layer and the response
  rendered into the console output (`dock_console_run.png`); the remaining
  manual UI surfaces (menus, dialogs) are still covered headlessly.

## 8. Notes for the operator

* Both QGIS instances are **left running and bridge-enabled** (autostart=true).
  Ports: QGIS 3.44 → 9877/9879, QGIS 4.2.3 → 9887/9889.
* Default posture is secure and was restored after the demo: both instances are
  back on `allow_execute=false` (it was temporarily enabled during the live demo
  via `system.config_set`; flip it in the dock to re-enable
  Processing/`python.exec`).
* Both instances share the same `paths.allow` (`~`, `:TEMP:`, `:PROJECT:`,
  `...\verification`); the whitelist is enforced per instance — a render to a
  non-allow-listed path fails with the exact path in the error (observed live
  on 4.2.3 during the final sweep).
* Diagnostics for any future session: `system.diagnose`, `system.metrics`,
  `system.audit_tail`, `session.journal_list`.

## 9. Command coverage sweep (2026-10-09 00:05–00:35)

Beyond the 63-check smoke, a dedicated sweep drives the **whole registered
command surface** (194 commands) with real fixtures (vector/join/raster
layers, layout + atlas, csv/gpkg/geojson, a loopback file server):

* **Headless suite** — `scripts/qgis_command_coverage.py`: **149 passed / 0
  failed** on 3.44.15, 4.2.3 and the 4.3.0-dev build
  (`logs/coverage_3.44.15-Solothurn_20261009-000738.json`,
  `logs/coverage_4.2.3-Belém_do_Pará_20261009-001107.json`,
  `logs/coverage_4.3.0-Master_20261009-001122.json`).
* **Live suite** — canvas / UI / themes / bookmarks / plugins + the zoom
  family against the running GUIs: **33/33 on both instances**
  (`logs/live_cov_9877_20261009.json`, `logs/live_cov_9887_20261009.json`);
  `layers.active_set` was additionally verified by hand on both.
* Union of smoke + both suites reaches **192 of 194 commands** at this point.
  The two deliberately not executed are `db.connection_add` / `db.import_layer`
  (they would rewrite the operator's saved database connections — their read
  paths and refusal paths are covered instead). **§10 lifts this to 194/194**
  with a sandboxed sweep.
* The headless soft-limits (`layers.active_set`, `bookmarks_zoom`,
  `themes_add/apply` — all GUI-session features) are verified in the live
  suite instead.

### Real defects found & fixed by the sweep (17 across 21 commands)

| # | Command(s) | Symptom | Fix |
|---|---|---|---|
| 1 | `layers.order`, `group_move_layer`, `group_remove` | detaching layer-tree nodes made the registry bridge delete the layers outright | clone-insert-then-remove node pattern; `layerId()` for group children |
| 2 | `layers.set_source` | `setDataSource()` returns `None` on 3.44 → false failure | accept `isValid()` when not `False`; ogr/gdal provider fallback |
| 3 | `layers.add_delimited` | percent-encoded URI breaks the delimitedtext provider | raw `delimiter`/`crs` + explicit `type=csv` |
| 4 | `layout.info` | crash on shape items (no `.id()`) | guarded `getattr` |
| 5 | `layout.add_table` | multi-frame placement API hidden in PyQt | `addMultiFrame` + `QgsLayoutFrame`; guarded `id()` |
| 6 | `layout._place` | `attemptMove` absent on some item classes | fallback chain `attemptMove → setPosition → setPos` |
| 7 | `style.copy` | `setStyleManager` not exposed | `QgsMapLayerStyle.readFromLayer/writeToLayer` |
| 8 | `session.edit_rollback` | `rollback()` is `rollBack()` in the binding | spelling-safe call |
| 9 | `raster.colormap_get` | `shader.type()` removed in 3.44 | `colorRampType()` fallback |
| 10 | `project.bookmarks_add` | `addBookmark()` returns an `(id, ok)` tuple → unusable id | unpack + duplicate guard |
| 11 | `project.bookmarks_zoom` | `QgsBookmark.crs()` not exposed | CRS taken from the referenced extent |
| 12 | `project.metadata_set` | 3.44 `setKeywords()` now takes a vocabulary dict | try list → `{"": [...]}` |
| 13 | `project.themes_add/apply` | 3.44 needs `MapThemeRecord` + a real tree model (a `None` model crashes natively) | live tree-view model + record API; clean refusal headless |
| 14 | `system.shutdown` | reply lost — server stopped before the response was sent | deferred stop (250 ms) |
| 15 | `plugins.enable/disable` | `startPlugin`/`stopPlugin` missing on some builds | `loadPlugin`/`unloadPlugin` fallbacks |
| 16 | `canvas.scale_set`, `canvas.center_set` | `QgsMapCanvas.setScale` not exposed | `zoomScale` fallback |
| 17 | `features.zoom_selection` | single-point selections have zero-area bounds → "nothing selected" | `selectedFeatureCount()` truth test + zero-area padding |

## 10. Sandbox completion — 194/194 exercised on every runnable build (2026-10-09 00:14–00:25)

The last two commands that were skipped by design (`db.connection_add`,
`db.import_layer`) are now exercised for real, without touching operator data:

* `scripts/qgis_command_coverage.py --sandbox-db` refuses to run the two
  commands unless `QGIS_CUSTOM_CONFIG_PATH` redirects
  `QgsApplication.qgisSettingsDirPath()` into `_scratch/cov_sandbox` (run-time
  interlock checked against the live settings directory).
* The redirect is proven inside the run: `sandbox.settings_readback` (the saved
  connection path equals the sandbox GeoPackage) and `sandbox.evidence`
  (`settings_file = …/cov_sandbox/cfg_344/profiles/default/QGIS/QGIS3-piterm-cov.ini`).
* Operator profiles stay byte-identical: all six `*.ini` files under
  `%APPDATA%\QGIS\QGIS3|QGIS4` hashed before/after — **0 diffs, 0 `piterm_cov`
  strings** (`logs/user_profile_hash_baseline.json` vs `…_after.json`).
* Results per build: **177 passed / 0 hard failures / 4 known GUI-session soft
  items**; `exercised_headless` 133/194 with `skipped_by_design` now empty; the
  union with the smoke + live sweeps is the full **194/194**.
  Logs: `logs/coverage_3.44.15-Solothurn_20261009-002212.json`,
  `logs/coverage_4.2.3-Belém_do_Pará_20261009-002339.json`,
  `logs/coverage_4.3.0-Master_20261009-002310.json`.
* Extra depth on the db surface (first positive exercise of `db.tables` /
  `db.sql` / `db.add_layer`): import → SQL count 5; overwrite with a different
  layer → count 2 (real replace, siblings intact); refusal when the table
  exists without `overwrite=true`; connection add/list/remove round-trip for
  gpkg + postgres, cleanup verified. Archive:
  `logs/cov_sandbox_import_34415.gpkg` (the surviving 2-row table; siblings
  `cov_sandbox_import_40203.gpkg` / `…_40300.gpkg` from the other builds).

* Redeploy regression on the fixed code (deployed copies hash-equal to the
  repo): smoke **63/63** on 3.44.15 & 4.2.3
  (`logs/smoke_3.44.15-Solothurn_20261009-002443.json`,
  `logs/smoke_4.2.3-Belém_do_Pará_20261009-002500.json`) and live **33/33** on
  both instances (`logs/live_cov_9877_20261009-0030.json`,
  `logs/live_cov_9887_20261009-0030.json`).

### Defect #18 — `db.import_layer` was broken on every verified build

`QgsAbstractDatabaseProviderConnection.VectorLayerOptions` does not exist and
`createVectorLayer()` was removed from the connection class on 3.44 / 4.2.3 /
4.3.0-dev, so every import attempt failed. The modern exporter alternative was
probed and rejected for file targets: its `overwrite` flag recreates the whole
file and silently drops sibling layers. Fix: file kinds (gpkg/spatialite) write
via `QgsVectorFileWriter.CreateOrOverwriteLayer`; server kinds keep an
`QgsVectorLayerExporter` fallback for builds without `createVectorLayer`.

### Housekeeping

* Coverage script: QGIS-native GeoPackage fixture (raw `osgeo` calls are
  unreliable inside a QGIS process), per-run port overrides (`PITERM_COV_PORT`
  / `PITERM_COV_HTTP_PORT` / `PITERM_COV_FETCH_PORT`), failure `detail`
  propagation, and zero `ResourceWarning`s under `-X dev` (the manifest handle
  and the HTTP socket were previously left unclosed).
* QGIS 4.2.0 (`C:\Program Files\QGIS 4.2.0`) is an uninstall leftover — only
  `bin\setup.bat` + `etc\preremove-conf.bat`, no executables — so it is not a
  runnable build and is excluded; the verified matrix is 3.44.15 / 4.2.3 /
  4.3.0-dev.

## 11. One-command verification (`scripts/verify_all.ps1`, 2026-10-09 00:30)

The live-GUI sweep now ships in-repo (`scripts/qgis_live_coverage.py` —
portable paths, per-check JSON, non-zero exit on failure) and a single entry
point runs the whole matrix:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_all.ps1 -Sandbox
```

* pipeline: pytest → smoke (63) per build → sandboxed command-coverage per
  build → live sweep per running instance;
* every suite's full console output is teed into
  `verification/logs/verify_all_<stamp>.log` (61 KB for the reference run),
  next to the usual per-suite JSONs; exit code 0 only when everything passed;
* switches: `-Versions`, `-LivePorts`,
  `-SkipUnit / -SkipSmoke / -SkipCoverage / -SkipLive`.
* per-instance tokens: `-LiveTokens 9887=<secret>` (a profile with `auth.enabled`
  would otherwise answer the sweep with `E_AUTH`); list values survive the
  `powershell -File` argument passing (comma/space/semicolon), and a requested
  version whose interpreter is missing now **fails** the run instead of being
  silently skipped (`-AllowMissing` opts back into the old behaviour).

Reference run — whole matrix in ≈36 s (`logs/verify_all_20261009-003025.log`):

| suite | 3.44.15 | 4.2.3 | 4.3.0-dev |
|---|---|---|---|
| pytest (36 tests) | `36 passed in 2.51s` | — | — |
| smoke 63 | ok `…003031` | ok `…003043` | ok `…003054` |
| coverage, sandboxed db (177 pass / 0 hard fail each) | `…003037` | `…003051` | `…003059` |
| live 33 | 9877 ok `…003100` | 9887 ok `…003101` | — |

The sandbox-interlock units (`tests/test_coverage_sandbox.py`) guard the
`--sandbox-db` safety contract: the settings root must live under
`_scratch/cov_sandbox` (prefix tricks and mismatched env roots are rejected),
so this command can never write to operator profiles. Suite runtimes measured
by the run: pytest ≈2.5 s, smoke ≈5 s, coverage ≈6 s, live ≈2 s per instance.

---

## 12. Operating manual + two defects found *by writing it* (2026-10-09 23:2x–23:5x)

Writing the "how do I actually use this" manual turned into a real test of the
client-setup path and of the policy read path. Both had defects; both are fixed,
unit-pinned and re-verified live.

### 12.1 Delivered

* **`docs/USAGE.md` + `docs/USAGE.zh-CN.md`** — install → start → connect
  (MCP stdio / HTTP / raw TCP / CLI) → call (recipes) → security switches →
  verification → troubleshooting → FAQ, plus README/INSTALL/SKILL cross-links.
* **`system.client_setup`** (new command, catalog **194 → 195**): returns the
  resolved client entry point and ready-to-paste configs for 16 clients, from a
  shell or from an agent — `{"client": "cursor"}` selects one. Exercised by the
  headless sweep (see `_client_setup()` in `scripts/qgis_command_coverage.py`).

### 12.2 Defect #19 — the client configurator handed out a dead path

`ui/configurator.py` built the snippet path as
`<repo>/mcp/piterm_qgis_mcp/min.py` (the file is `minimal.py`) and derived the
repo root by counting `dirname()` levels — which, for a plugin installed under
`…\profiles\default\python\plugins\`, resolves to `…\profiles\default\python`.
Every copied config was therefore broken, silently.

Fix: resolution moved into QGIS-free `plugin/piterm_qgis_ai/client_paths.py`
(`configured root → walk up → bundled copy`, with `source` /
`full_available` / a human `note`); `scripts/install.ps1` now bundles
`minimal.py` next to the plugin and records `meta.repo_root` in the profile ini;
the dialog shows the resolution status (red when nothing is found) instead of
emitting a path; the FastMCP variant is only offered when a real checkout exists.
Pinned by `tests/test_client_paths.py` (11 tests) and by the coverage sweep.

### 12.3 Defect #20 — policy switches could not be turned off

QgsSettings round-trips Python `False` as the **string** `"false"`, and
`bool("false")` is `True`. `SecurityConfig.reload()` used bare `bool(...)`, so a
stored `mode.allow_execute=false` read back as *enabled* (same for
`mode.read_only` and `mode.confirm_destructive`). Observed live: after reloading
the fixed plugin the same stored `"false"` immediately reported
`allow_execute: false`.

Fix: `security.coerce_value()` is now the single coercion point for
`SecurityConfig.reload`, `system.config_get` and `system.config_set`
(QgsSettings strings, ini edits and JSON payloads all agree); `limits.rate_per_sec`
default became a float so it coerces numerically; `config_set` rejects
non-boolean text instead of guessing. Pinned by `tests/test_settings_coercion.py`
(30 cases incl. the exact `"false"` round-trip and end-to-end policy checks).

### 12.4 Verification — reference run, whole matrix in ≈30 s

`logs/verify_all_20261009-233739.log` (126 KB, full console transcript),
`VERIFY ALL OK - every requested suite passed` (exit 0):

| suite | 3.44.15 | 4.2.3 | 4.3.0-dev |
|---|---|---|---|
| pytest (**79 tests**) | `79 passed in 2.5s` | — | — |
| smoke 63 | ok `…233746` | ok `…233754` | ok `…233803` |
| coverage (sandboxed db) — 182 checks, **178 pass / 4 soft / 0 hard**, `exercised 134/195`, `sandbox_db.active=true` | `…233750` | `…233800` | `…233807` |
| live 33 (9877 no token, 9887 per-port token) | ok `…233807` | ok `…233808` | — |

Both QGIS instances were running the freshly installed plugin (3.44.15 →
9877/9879, 4.2.3 → 9887/9889). The four soft failures are the GUI-only items
headless cannot do (`layers.active_set`, `project.bookmarks_zoom`,
`project.themes_add`, `project.themes_apply`) — all four pass in the live sweep.

Three earlier runs in the same window failed and are kept as evidence of the
tooling/environment issues they exposed: `verify_all_20261009-233000.log`
(second QGIS not yet started → `live:9887`), `…233239.log` (the
`-LivePorts 9877,9887` parsing bug — PowerShell read the pair as the integer
98779887 → `live:98779887`), `…233338.log` (the 9877-carrying QGIS window was
closed mid-run → `live:9877`) and `…233554.log` (9887 answered `E_AUTH` because
that profile enforces a token → hence `-LiveTokens`).
