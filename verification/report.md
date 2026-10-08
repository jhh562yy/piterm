# Verification report — Piterm QGIS AI 1.0.0 (contract CT-20261008-01)

**Date:** 2026-10-08 · **Status:** all AI-side lights green; awaiting user-side
acceptance in the contract module.
**Deliverable:** `G:\AIato\pi\piterm-qgis-ai` — 92 files, ~22 000 lines of
source/docs (excluding `_reference/` research checkouts and `.venv`).

---

## 1. What was built

| Component | Path | Notes |
|---|---|---|
| QGIS plugin | `plugin/piterm_qgis_ai/` | 0-thread bridge on the Qt event loop; **194 commands** (generated catalog), 16 domain handler modules, dock UI, client configurator |
| MCP server | `mcp/piterm_qgis_mcp/` | `minimal.py` (stdlib-only client + MCP stdio server + CLI, sha16 `A365F2500C8B98AC`), `server.py` (FastMCP for mcp 1.x / MCPServer for mcp 2.x), dynamic tools from the live catalog |
| Skill | `skill/piterm-qgis-ai/` | `SKILL.md` + `piterm_qgis.py`; **installed into this environment** as `skill://piterm-qgis-ai` |
| Installers | `scripts/install.ps1`, `install.py` | multi-profile deploy, backups, ini enable, uninstall |
| Docs | `docs/` | RESEARCH / ARCHITECTURE / PROTOCOL / TOOLS (generated, 194 commands) / SECURITY / PERFORMANCE / INSTALL / COMPAT / CHANGELOG |
| Tests | `tests/` | 26 unit tests (wire, security, manifest AST, fake-bridge round-trip) |

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
| R2 | Plugin loads headless on QGIS 3.44 **and** 4.2 | `scripts/qgis_headless_smoke.py` → **63/63 checks** each: `verification/logs/smoke_3.44.15-Solothurn_20261008-234023.json`, `smoke_4.2.3-Belém_do_Pará_20261008-234106.json` | ✅ |
| R3 | MCP server handshake + tool calls | minimal stdio: `tests/test_min_roundtrip.py`; official SDK probe: `scripts/mcp_fastmcp_probe.py` against live QGIS → `initialize ok (protocol 2025-11-25)`, **196 tools**, `system_ping`/`layers_list`/`piterm_status` all answered | ✅ |
| R4 | Skill/CLI performs real operations | live QGIS 3.44 via CLI: `layers.add_memory`, `features.insert` (5), `layers.zoom_to`, `canvas.render`, `canvas.screenshot`, `processing.execute native:buffer`, `session.checkpoint_create`; HTTP `/rpc` ping; both GUI instances answering (9877 / 9887) | ✅ |
| R5 | Deploy scripts with backup/uninstall | `scripts/install.ps1` deployed to both profiles (39 files each), ini enable + backups (`...plugins\piterm_qgis_ai.bak-20261008-233919`, `QGIS3.ini.bak-piterm-*`), autostart configured for both | ✅ |
| R6 | Security & control chain | read-only blocks writes (`E_FORBIDDEN`), token reject/accept (`E_AUTH` / ok), path allow/deny (`E_PATH_DENIED`), audit + journal + checkpoint + undo verified in smoke; sandbox rules unit-tested | ✅ |
| R7 | Docs complete + generated TOOLS ≥120 | `docs/TOOLS.md` generated with **194 commands**; README/ARCHITECTURE/PROTOCOL/SECURITY/PERFORMANCE/INSTALL/COMPAT/CHANGELOG/RESEARCH present | ✅ |

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

* QGIS 3.28–3.40 declared supported via the compat layer; only 3.44.15 and
  4.2.3 were executed here (no other installs on this machine).
* macOS/Linux paths in `install.py` are untested on this machine.
* `streamable-http` transport of the full MCP server is implemented but the
  end-to-end probe used stdio (the transport clients use by default).
* GUI screenshots were taken from the desktop; the dock's interactive console
  was exercised headlessly (`system.help`/capabilities) rather than by UI clicks.

## 8. Notes for the operator

* Both QGIS instances are **left running and bridge-enabled** (autostart=true).
  Ports: QGIS 3.44 → 9877/9879, QGIS 4.2.3 → 9887/9889.
* Default posture is secure: no token on loopback, `allow_execute=false` until
  flipped in the dock (it was enabled during the live demo via
  `system.config_set` and persisted as a demo; flip it back in the dock if you
  prefer the stricter default).
* Diagnostics for any future session: `system.diagnose`, `system.metrics`,
  `system.audit_tail`, `session.journal_list`.
