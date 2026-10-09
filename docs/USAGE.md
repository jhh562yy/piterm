# How to use Piterm QGIS AI

The complete operating manual: install → start → connect → call → secure → verify.
Everything here is copy-pasteable; the paths are this machine's (`G:\AIato\pi\piterm-qgis-ai`).
Swap them for yours — the plugin never hard-codes them.

> Short on time? §1 (60-second path) and §6 (recipes) are enough to be productive.
> Everything else is the "what if it goes wrong / what if I need control" half.

---

## 0. Mental model (read once)

There are three moving parts, and only one of them is a server:

```
  your agent / shell / script
        │                                  ①  CLI + skill   (zero dependencies)
        │  ┌──────────────────────────────┐ ②  MCP stdio     (minimal or FastMCP)
        ├──┤  MCP server / CLI / curl     │ ③  direct wire   (TCP 9877 / HTTP 9879)
        │  └──────────────────────────────┘
        ▼  127.0.0.1:9877  (frames or NDJSON)   ·   127.0.0.1:9879 (HTTP JSON-RPC)
  ┌─────────────────────────────────────────────────────────────┐
  │ QGIS process (the only place PyQGIS may run)                │
  │  plugin  piterm_qgis_ai                                     │
  │   QTcpServer on the Qt event loop — **zero threads**        │
  │   dispatcher: auth → policy → validate → handler → audit    │
  │   195 commands · jobs · cursors · checkpoints · journal     │
  └─────────────────────────────────────────────────────────────┘
```

Three consequences worth internalising:

1. **QGIS must be open and the bridge started** for anything to work. If either is
   missing you get `E_BRIDGE_DOWN`.
2. **The bridge is a local automation surface.** Whoever can reach the port can drive
   QGIS. Defaults are loopback-only; tokens/scopes/confirm/paths are there when you
   need them (§7).
3. **The command catalog is generated** from the live plugin registry, so docs and
   reality cannot drift: `docs/TOOLS.md`, `mcp/piterm_qgis_mcp/manifest.json` and
   `system.capabilities` all come from the same source. Ask the bridge itself:
   `system.help {"query": "..."}`.

---

## 1. 60-second path

```powershell
cd G:\AIato\pi\piterm-qgis-ai

# 1 · deploy into every detected QGIS profile (backs up, enables, bundles the client)
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -All

# 2 · open QGIS → click the π toolbar button (or dock → "Start server")
#     the status dot turns green: "running · frame 9877 / http 9879 · 0 client(s)"

# 3 · talk to it from any shell — standard library only, no pip, no MCP
python mcp\piterm_qgis_mcp\minimal.py ping
python mcp\piterm_qgis_mcp\minimal.py call system.diagnose '{}'
python mcp\piterm_qgis_mcp\minimal.py screenshot shot.png
```

Expected: `{"pong": true, "plugin": "1.0.0"}`, a `diagnose` block with `"ok": true`, and a
PNG on disk. If any of those fail, jump to §9.

---

## 2. Install the plugin

### Automatic (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1          # profile "default"
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -All     # every profile
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Profile default,custom
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoEnable    # copy only
```

Per profile it:

1. moves an existing `piterm_qgis_ai` folder to `.bak-<stamp>`;
2. copies the plugin (without `__pycache__`) and the generated `manifest.json`;
3. **bundles the zero-dependency client** at
   `<plugin>\mcp\piterm_qgis_mcp\minimal.py` so *Client setup* works even when the
   checkout is not next to the installed plugin;
4. records the checkout in the profile ini ([`piterm_qgis_ai`] `meta.repo_root=…`,
   ini backed up first) and adds `piterm_qgis_ai=true` to `[PythonPlugins]`.

### Automatic (Linux/macOS/CI)

```bash
python install.py [--all-profiles] [--profile default] [--uninstall] [--list]
```

### Manual

1. Copy `plugin/piterm_qgis_ai/` into your profile's `python/plugins/`
   (*Settings → User Profiles → Open active profile folder*). Optionally copy
   `mcp/piterm_qgis_mcp/minimal.py` to `<plugin>/mcp/piterm_qgis_mcp/`.
2. Restart QGIS → *Plugins → Manage → enable "Piterm QGIS AI"*.
3. Toolbar π button or dock → **Start server**.

Requirements: QGIS **3.28+** (verified on 3.44.15 LTR, 4.2.3, 4.3.0-dev), Python **3.9+**
for the CLI/MCP (standard library only). `mcp>=1.0` is optional and only needed for the
FastMCP server.

---

## 3. Start the bridge

| | |
|---|---|
| **Toolbar** | π button → start/stop |
| **Dock** | *Plugins → Piterm QGIS AI → Piterm QGIS AI* — status dot, clients, log, console, metrics |
| **Ports** | frame/NDJSON **9877**, HTTP **9879** (change in the dock; they persist) |
| **Auto-start** | dock checkbox *Auto-start with QGIS* (`mode.autostart=true`) |
| **Bind** | default `127.0.0.1`; a non-loopback host is **refused** unless a token is set |

The dock is also where you: kick clients, filter the live log, run commands from the
built-in console (autocomplete + JSON params), read per-command telemetry, and open
**Client setup…**.

Health from the outside, without any client library:

```powershell
python mcp\piterm_qgis_mcp\minimal.py --port 9877 ping      # TCP frame/NDJSON
curl -s http://127.0.0.1:9879/health                        # HTTP
curl -s http://127.0.0.1:9879/info                          # protocol + features
```

Both ports belong to the QGIS process; if QGIS is closed, both are gone (that is
`E_BRIDGE_DOWN`, not a bug).

---

## 4. Verify the installation (first-run checklist)

```powershell
python mcp\piterm_qgis_mcp\minimal.py call system.ping '{}'          # {"pong": true}
python mcp\piterm_qgis_mcp\minimal.py call system.diagnose '{}'      # "ok": true + checks[]
python mcp\piterm_qgis_mcp\minimal.py call system.help '{"query":"render"}'
python mcp\piterm_qgis_mcp\minimal.py call layers.count '{}'         # 0 on an empty project
python mcp\piterm_qgis_mcp\minimal.py call canvas.render '{"path":"G:/AIato/pi/_scratch/map.png"}'
```

`system.diagnose` is the single best "is everything sane" button: version, ports, client
count, per-command counters, security posture and a `checks` list with pass/fail.

The full self-test (unit + headless + sandboxed + live GUI) is one command — see §8.

---

## 5. Connect your client

### 5.1 Let the bridge write the config for you

In QGIS: **Piterm QGIS AI → Client setup…** — pick your client, hit **Copy** (or *Save to
file*, which backs up and JSON-merges into the existing config). The snippets are
generated from the live host/ports/token, so they always match the running bridge.

From a shell or from an agent, the same thing over the wire:

```powershell
python mcp\piterm_qgis_mcp\minimal.py call system.client_setup '{}'                  # all clients
python mcp\piterm_qgis_mcp\minimal.py --json call system.client_setup '{"client":"cursor"}'
```

It returns the resolved client entry point (`min_script`, `repo_root`, `source`,
`full_available`, a human `note`) plus ready-to-paste configs for: Claude Desktop
(minimal **and** full), Claude Code, Cursor, VS Code, Gemini CLI, Qwen Code, Kimi,
Copilot CLI, LM Studio, opencode, Windsurf, Codex (TOML), Hermes (bat+YAML), piterm
terminal, and a no-MCP "direct" cheatsheet.

### 5.2 Minimal MCP server (recommended default: zero dependencies)

```json
{ "mcpServers": { "piterm-qgis": {
    "command": "python",
    "args": ["G:/AIato/pi/piterm-qgis-ai/mcp/piterm_qgis_mcp/minimal.py", "--mcp-stdio"],
    "env": { "PITERM_QGIS_TOKEN": "<only if auth is enabled>" } } } }
```

The minimal server proxies the *whole* catalog (195 tools) over stdio; it just publishes
simpler JSON schemas than FastMCP.

### 5.3 Full FastMCP server (richer schemas, resources, prompts)

```json
{ "mcpServers": { "piterm-qgis": {
    "command": "uv",
    "args": ["--directory", "G:/AIato/pi/piterm-qgis-ai", "run", "--no-sync",
             "python", "-m", "piterm_qgis_mcp"] } } }
```

Needs `uv` and `pip install "mcp>=1.0"`. `system.client_setup` only offers this variant
when a real checkout is present (`full_available: true`), because pointing a client at a
missing module is worse than offering nothing.

### 5.4 No MCP at all (skill / shell / pipeline)

```powershell
python mcp\piterm_qgis_mcp\minimal.py call layers.list '{}'
python mcp\piterm_qgis_mcp\minimal.py screenshot shot.png
python mcp\piterm_qgis_mcp\minimal.py batch '[{"type":"system.ping"},{"type":"layers.count"}]'

# HTTP JSON-RPC
curl -s http://127.0.0.1:9879/rpc -H "Content-Type: application/json" -d '{"type":"system.ping"}'

# raw TCP, one JSON object per line (any shell / netcat)
echo {"type":"layers.list"} | ncat 127.0.0.1 9877
```

### 5.5 Environment variables (both servers and the CLI)

| var | default | meaning |
|---|---|---|
| `PITERM_QGIS_HOST` | `127.0.0.1` | bridge host |
| `PITERM_QGIS_PORT` | `9877` | TCP port |
| `PITERM_QGIS_TOKEN` | – | shared secret when `auth.enabled` |
| `PITERM_QGIS_TIMEOUT` | `60` | per-call seconds |
| `PITERM_QGIS_GZIP` | `0` | `1` → accept gzip responses |
| `PITERM_QGIS_TOOL_MODE` | `granular` | `compound` for clients with tool limits |
| `PITERM_QGIS_TRANSPORT` | `stdio` | `streamable-http` for the full server |

---

## 6. Calling commands

### 6.1 The CLI surface

```
piterm-qgis [--host H] [--port P] [--token T] [--timeout S] [--client-name N]
            [--compress] [--json]
            {ping | hello | call | tools | screenshot | mcp-stdio | batch}
```

Rules of the game: **params are JSON, results are JSON**, and failures are
`{"ok": false, "error": {"code": "E_…", "message": "…"}}` with non-zero exit (script it
freely in CI).

### 6.2 Recipes (all runnable)

```powershell
# --- discovery -------------------------------------------------------------
python mcp\piterm_qgis_mcp\minimal.py call system.hello '{}'                  # handshake
python mcp\piterm_qgis_mcp\minimal.py call system.capabilities '{}'           # full catalog
python mcp\piterm_qgis_mcp\minimal.py call system.help '{"query":"checkpoint"}'
python mcp\piterm_qgis_mcp\minimal.py tools                                   # MCP tool list

# --- load & inspect --------------------------------------------------------
python mcp\piterm_qgis_mcp\minimal.py call layers.add_vector '{"path":"D:/data/roads.gpkg"}'
python mcp\piterm_qgis_mcp\minimal.py call layers.list '{}'
python mcp\piterm_qgis_mcp\minimal.py call layers.info '{"layer":"roads"}'
python mcp\piterm_qgis_mcp\minimal.py call layers.fields_list '{"layer":"roads"}'
python mcp\piterm_qgis_mcp\minimal.py call layers.count '{}'

# --- query, edit, undo (edits are real: checkpoint first if unsure) --------
python mcp\piterm_qgis_mcp\minimal.py call features.query '{"layer":"roads","filter":"\"lanes\" > 2","order_by":"length desc","limit":20}'
python mcp\piterm_qgis_mcp\minimal.py call features.insert '{"layer":"roads","features":[{"attributes":{"name":"new"},"geometry":{"wkt":"LINESTRING (0 0, 1 1)"}}]}'
python mcp\piterm_qgis_mcp\minimal.py call features.field_calculator '{"layer":"roads","field":"len2","expression":"$length*2"}'
python mcp\piterm_qgis_mcp\minimal.py call session.undo '{"layer":"roads","steps":1}'
python mcp\piterm_qgis_mcp\minimal.py call session.edit_status '{}'

# --- big reads without blowing memory (chunked cursors) --------------------
python mcp\piterm_qgis_mcp\minimal.py call features.cursor_open '{"layer":"roads","fields":["name"],"bbox":[0,0,1000,1000]}'
python mcp\piterm_qgis_mcp\minimal.py call features.cursor_fetch '{"cursor":"cur-1","n":500}'
python mcp\piterm_qgis_mcp\minimal.py call features.cursor_close '{"cursor":"cur-1"}'

# --- styling & labelling ---------------------------------------------------
python mcp\piterm_qgis_mcp\minimal.py call style.renderer_set '{"layer":"roads","kind":"categorized","field":"class"}'
python mcp\piterm_qgis_mcp\minimal.py call style.renderer_set '{"layer":"roads","kind":"graduated","field":"length","classes":5,"ramp":"Spectral"}'
python mcp\piterm_qgis_mcp\minimal.py call style.labeling_set '{"layer":"roads","field":"name","enabled":true}'
python mcp\piterm_qgis_mcp\minimal.py call style.qml_save '{"layer":"roads","path":"D:/out/roads.qml"}'

# --- see it (this is what "give the user a map" means) ---------------------
python mcp\piterm_qgis_mcp\minimal.py call layers.zoom_to '{"layer":"roads"}'
python mcp\piterm_qgis_mcp\minimal.py call canvas.render '{"width":1600,"height":900,"path":"D:/out/map.png"}'
python mcp\piterm_qgis_mcp\minimal.py screenshot D:/out/canvas.png

# --- Processing: sync (≤55 s) vs async (no limit) --------------------------
python mcp\piterm_qgis_mcp\minimal.py call processing.execute '{"algorithm":"native:buffer","parameters":{"INPUT":"roads","DISTANCE":50,"OUTPUT":"memory:buf"}}'
python mcp\piterm_qgis_mcp\minimal.py call processing.start   '{"algorithm":"native:dissolve","parameters":{"INPUT":"plots","FIELD":["owner"],"OUTPUT":"memory:diss"},"load_results":true}'
python mcp\piterm_qgis_mcp\minimal.py call job.get     '{"id":"job-1"}'
python mcp\piterm_qgis_mcp\minimal.py call job.log     '{"id":"job-1"}'
python mcp\piterm_qgis_mcp\minimal.py call job.cancel  '{"id":"job-1"}'
python mcp\piterm_qgis_mcp\minimal.py call processing.algorithms_search '{"query":"zonal"}'

# --- raster ---------------------------------------------------------------
python mcp\piterm_qgis_mcp\minimal.py call raster.stats '{"layer":"dem"}'
python mcp\piterm_qgis_mcp\minimal.py call raster.zonal_stats '{"raster":"dem","zones":"plots","stats":["mean","max"]}'
python mcp\piterm_qgis_mcp\minimal.py call raster.calculator '{"expression":"\"dem\" * 2","output":"memory:dem2"}'

# --- layouts & atlas ------------------------------------------------------
python mcp\piterm_qgis_mcp\minimal.py call layout.create '{"name":"A3"}'
python mcp\piterm_qgis_mcp\minimal.py call layout.add_map '{"layout":"A3","rect":[10,10,380,270]}'
python mcp\piterm_qgis_mcp\minimal.py call layout.add_legend '{"layout":"A3"}'
python mcp\piterm_qgis_mcp\minimal.py call layout.export '{"layout":"A3","path":"D:/out/a3.pdf"}'
python mcp\piterm_qgis_mcp\minimal.py call layout.atlas_configure '{"layout":"A3","layer":"plots"}'
python mcp\piterm_qgis_mcp\minimal.py call layout.atlas_export '{"layout":"A3","path":"D:/out/atlas.pdf"}'

# --- SQL (read by default; writes need allow_write + a writable source) ----
python mcp\piterm_qgis_mcp\minimal.py call db.connections_list '{}'
python mcp\piterm_qgis_mcp\minimal.py call db.tables '{"connection":"pg_main"}'
python mcp\piterm_qgis_mcp\minimal.py call db.sql '{"connection":"pg_main","sql":"SELECT count(*) FROM roads"}'

# --- multi-step workflow with $N references, dry-run first ----------------
python mcp\piterm_qgis_mcp\minimal.py call session.workflow_run '{"dry_run":true,"steps":[{"type":"layers.add_vector","params":{"path":"D:/data/a.gpkg"}},{"type":"processing.execute","params":{"algorithm":"native:buffer","parameters":{"INPUT":"$0.result.loaded_layers.0.id","DISTANCE":100,"OUTPUT":"memory:buf"}}}]}'
python mcp\piterm_qgis_mcp\minimal.py call session.workflow_run '{"steps":[...],"stop_on_error":true}'

# --- checkpoints / journal (the safety net) -------------------------------
python mcp\piterm_qgis_mcp\minimal.py call session.checkpoint_create '{"name":"before-cleanup"}'
python mcp\piterm_qgis_mcp\minimal.py call session.checkpoint_list '{}'
python mcp\piterm_qgis_mcp\minimal.py call session.checkpoint_restore '{"checkpoint":"ckpt-20261009-233000-1"}'
python mcp\piterm_qgis_mcp\minimal.py call session.journal_list '{}'
python mcp\piterm_qgis_mcp\minimal.py call session.journal_export '{"path":"D:/out/session.jsonl"}'

# --- one round-trip for many commands ------------------------------------
python mcp\piterm_qgis_mcp\minimal.py call system.batch '{"commands":[{"type":"layers.list"},{"type":"layers.count"}]}'

# --- escape hatch (auditable sandbox; exec_full is gated, see §7) ---------
python mcp\piterm_qgis_mcp\minimal.py call python.exec '{"code":"print(project.fileName())"}'
```

Every response carries `meta.t_ms` (per-command timing), so you can profile from the
caller side without a profiler. `system.metrics` gives totals plus the top time consumers.

### 6.3 From your own Python

```python
import sys
sys.path.insert(0, r"G:/AIato/pi/piterm-qgis-ai/mcp")
from piterm_qgis_mcp.minimal import BridgeClient          # stdlib only

bridge = BridgeClient(host="127.0.0.1", port=9877)
print(bridge.ping())
print(bridge.call("layers.list", {}))
print(bridge.call("canvas.render", {"path": "D:/out/map.png", "width": 1600}))
```

The skill ships the same core: `python skill/piterm-qgis-ai/piterm_qgis.py ping`.

---

## 7. Security & control

Defaults are "convenient on a single-user machine, honest about the rest": loopback
bind, no token, writes allowed, `execute` allowed, path allow-list `~` · `:TEMP:` ·
`:PROJECT:`, audit on, `python.exec_full` off.

| Control | How to turn it on | Effect |
|---|---|---|
| **Token** | dock *Generate* + *Enforce token*, or `system.config_set {"key":"auth.enabled","value":true}` after setting `auth.token` | every request needs the token; 5 bad tries on one connection → `E_AUTH_LOCKED`; non-loopback binds become possible |
| **Read-only** | dock checkbox, or `mode.read_only=true` | all write/destructive/execute commands → `E_FORBIDDEN` |
| **No code execution** | `mode.allow_execute=false` | `python.exec*` → `E_FORBIDDEN` |
| **Confirm destructive** | `mode.confirm_destructive=true` | destructive commands need `"__confirm": true` |
| **Path allow/deny** | `paths.allow` / `paths.deny` | `E_PATH_DENIED` with the active patterns; deny wins |
| **Rate/clients** | `limits.rate_per_sec`, `limits.max_clients` (16) | `E_BUSY`; `system.clients_kick` |
| **Full Python** | `dangerous.allow_full_python=true` **and** `mode.allow_execute=true` | `python.exec_full` = arbitrary code = your account. Treat as opening a shell |

Reading and writing policy at runtime:

```powershell
python mcp\piterm_qgis_mcp\minimal.py call system.config_get '{}'                     # effective values + security block
python mcp\piterm_qgis_mcp\minimal.py call system.config_set '{"key":"mode.read_only","value":true}'
python mcp\piterm_qgis_mcp\minimal.py call system.diagnose '{}'                       # "security" in one glance
```

Two sandboxes, deliberately different: `python.exec` allows `qgis`/`math` imports only
and blocks `eval/exec/open/getattr/__import__`-style escapes; `python.exec_full` allows
everything and is gated by an extra switch. Prefer real commands over `python.exec`:
they validate, are auditable and are undo-able.

Forensics live in the profile: `system.audit_tail` (JSONL, 8 MB rotation),
`session.journal_list` / `session.journal_export` (replayable mutation log),
`session.checkpoint_*` (whole-project snapshots, memory layers included),
`system.messages` (QGIS message log ring). Incident response: kick clients →
read the audit tail → export the journal → restore a checkpoint → rotate the token.

Full threat model and recommended configurations: `docs/SECURITY.md`.

---

## 8. Verification

```powershell
# one command, whole matrix: pytest + smoke + sandboxed coverage + live GUI
powershell -ExecutionPolicy Bypass -File scripts\verify_all.ps1 -Sandbox

# quick pass (skip the heavy sweeps)
powershell -ExecutionPolicy Bypass -File scripts\verify_all.ps1 -SkipCoverage -SkipLive

# individual suites
.venv\Scripts\python -m pytest tests -q                                   # unit, no QGIS
"C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat" scripts\qgis_headless_smoke.py    # 63 checks
"C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat" scripts\qgis_command_coverage.py --sandbox-db
python scripts\qgis_live_coverage.py 9877                                 # 33 live checks vs a running bridge
```

Switches: `-Versions 3.44.15,4.2.3`, `-LivePorts 9877,9887`, `-SkipUnit`,
`-SkipSmoke`, `-SkipCoverage`, `-SkipLive`, `-Sandbox`.

What a green run looks like (reference, 2026-10-09): **79 unit tests**, smoke **63/63**
per build, sandboxed coverage **177 passed / 0 hard fail** per build with the whole
catalog exercised (headless + sandboxed db writers + live), live **33/33** per running
instance; the union covers **195/195 commands**. Runtime ≈ 40 s.

Evidence: `verification/logs/*.json` (per-check, per-suite) and the teed console
transcript `verification/logs/verify_all_<stamp>.log`; the narrative report is
`verification/report.md`. `-Sandbox` never touches your real profiles — it exercises the
connection-writing commands against a throwaway settings root and reports a hash diff.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `E_BRIDGE_DOWN` | QGIS not running, bridge not started, or wrong port — check the dock status dot and `--port` |
| Toolbar button missing | plugin not enabled: *Plugins → Manage → search "Piterm"* (or re-run `install.ps1`) |
| `"address in use"` on start | another QGIS/instance holds 9877 — change the port in the dock or stop the duplicate |
| Everything is `E_FORBIDDEN` | read-only mode or execute disabled — see the dock toggles / `system.config_get` |
| `E_PATH_DENIED` on save | extend `paths.allow` (`system.config_set`) or write under `:TEMP:` / `:PROJECT:` |
| `E_AUTH` / `E_AUTH_LOCKED` | token mismatch — copy the snippet from *Client setup* instead of hand-editing, then reconnect |
| Client sees no tools | it must spawn `python` on PATH with an **absolute** path to `minimal.py`; test that exact command in a shell first |
| Hermes-style agent dies with pydantic import errors | use the **Hermes** snippet — it launches through a `.bat` that clears `VIRTUAL_ENV`/`PYTHONPATH` |
| `python.exec` says `E_EXEC ... not allowed` | the sandbox refused an import/dunder; use a real command, or `python.exec_full` if you truly accept the risk |
| Command hangs then `E_TIMEOUT` | move long algorithms to `processing.start` and poll `job.get` |
| Big reads are slow | `features.cursor_open` + `cursor_fetch` paging, `fields` to trim columns, `bbox` to pre-filter |
| Ambiguous layer name (`E_AMBIGUOUS`) | `layers.find` → use the returned layer id |
| Mojibake in a Windows console | `chcp 65001`; the clients force UTF-8 themselves |
| QGIS log spammed with "listening failed" | duplicate bridge instances — stop the extras |
| *Client setup…* shows a red "No MCP client script found" | the checkout moved: re-run `install.ps1` (bundles a client copy) or set `meta.repo_root` |

Error-code cheat sheet: `E_BAD_REQUEST` `E_PARSE` `E_UNKNOWN_COMMAND` `E_AUTH`
`E_AUTH_LOCKED` `E_FORBIDDEN` `E_CONFIRM_REQUIRED` `E_NOT_FOUND` `E_AMBIGUOUS`
`E_STATE` `E_INVALID` `E_GEOMETRY` `E_CRS` `E_EXPRESSION` `E_IO` `E_PATH_DENIED`
`E_TIMEOUT` `E_BUSY` `E_PAYLOAD` `E_DISPATCH_GUARD` `E_CANCELLED` `E_INTERNAL` `E_EXEC`.
`E_NOT_FOUND` and `E_AMBIGUOUS` carry candidates in `detail` — read them instead of guessing.

---

## 10. Update / uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -All      # update (old kept as .bak-<stamp>)
# reload without restarting QGIS: Plugins → Piterm QGIS AI → Reload plugin
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
```

Uninstall removes the plugin folder, sets the ini key to `false` and clears
`meta.repo_root`; checkpoints/audit/journal under
`<profile>/piterm_qgis_ai/` are left in place on purpose. An agent can trigger the same
self-reload over the wire (the explicit guard is `plugins.reload piterm_qgis_ai` →
refused; use *Reload plugin*, or schedule
`qgis.utils.unloadPlugin/loadPlugin/startPlugin` from `python.exec`).

---

## 11. FAQ

**Why is there no threading?** PyQGIS is not thread-safe. The bridge runs a `QTcpServer`
on the Qt event loop; long work goes through `QgsTask`-backed jobs (`processing.start`).
That is why it does not randomly crash.

**How do I know the docs match the build?** They are generated from the live registry:
`scripts/export_manifest.py` writes `docs/TOOLS.md`, `mcp/piterm_qgis_mcp/manifest.json`
and `verification/manifest.json`; a unit test asserts the registry's shape.

**Can I use it from CI?** Yes for the headless parts: `qgis_headless_smoke.py` and
`qgis_command_coverage.py` boot their own `QgsApplication` and need no GUI. The GUI-only
items (theme/bookmark zoom, active layer switching) are covered by the live sweep.

**Which port do I point a client at — 9877 or 9879?** Clients that speak MCP speak
stdio (no port). `9877` is the raw socket (frames or NDJSON). `9879` is HTTP JSON-RPC
(`/rpc`, `/health`, `/commands`, `/events`). Same commands on all three.

**Where do the 195 commands come from?** `plugin/piterm_qgis_ai/handlers/` — 16 modules,
each method decorated with `@command(...)` declaring name, risk and tags. See
`docs/TOOLS.md` for the full table and `docs/ARCHITECTURE.md` for the dispatch pipeline.
