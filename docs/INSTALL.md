# Install & operate

> Day-to-day operation (connect a client, call commands, recipes, security
> switches, troubleshooting) lives in **[USAGE.md](USAGE.md)**
> ([中文版](USAGE.zh-CN.md)); this file is the install/deploy half.

## 0. TL;DR (this machine)

```powershell
cd G:\AIato\pi\piterm-qgis-ai

# deploy into every detected QGIS profile (backup + enable + verify)
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# open QGIS: the π toolbar button starts the bridge (or dock → Start server)
# verify from any shell (stdlib only):
python mcp\piterm_qgis_mcp\minimal.py ping
```

## 1. Requirements

* QGIS **3.28+** (tested on 3.44.15 LTR and 4.2.3; 4.3-dev builds load too).
* Python 3.9+ for the CLI / MCP server (uses only the standard library).
* Optional: `mcp` package for the full FastMCP server
  (`pip install "mcp>=1.0"`); the minimal server needs nothing.

## 2. Plugin deployment

### Automatic (recommended, Windows)

`scripts/install.ps1` finds every profile under
`%APPDATA%\QGIS\QGIS3\profiles` / `QGIS4` (and `QGIS3\profiles`-style layouts),
then for each:

1. backs up an existing `piterm_qgis_ai` folder to `.bak-<timestamp>`;
2. copies the plugin, strips `__pycache__`;
3. copies `manifest.json` (tool catalog for offline MCP use) when present and
   bundles the zero-dependency client to `<plugin>\mcp\piterm_qgis_mcp\minimal.py`,
   so **Client setup…** works even when the checkout is not next to the plugin;
4. records the checkout in the profile ini ([`piterm_qgis_ai`] `meta.repo_root=…`,
   backing the ini up first) and adds `piterm_qgis_ai=true` to `[PythonPlugins]`;
5. prints file counts.

Options: `-All` (all profiles, not just `default`), `-Profile default,custom`,
`-NoEnable` (don't touch the ini), `-Uninstall`.

### Automatic (Linux/macOS/CI)

```bash
python install.py [--all-profiles] [--profile default] [--uninstall] [--list]
```

### Manual

1. Copy `plugin/piterm_qgis_ai/` into your profile's `python/plugins/`
   (QGIS: *Settings → User Profiles → Open active profile folder*).
2. Restart QGIS, then enable **Piterm QGIS AI** in *Plugins → Manage*.
3. Click the π toolbar button or open the dock and press **Start server**.

## 3. Running the bridge

* Defaults: TCP **9877** (frames or NDJSON), HTTP **9879**. Change host/ports in
  the dock (they persist), or via `system.config_set`.
* Auto-start on QGIS launch: dock checkbox (*Auto-start with QGIS*).
* The dock shows: status, clients (with kick), live log (level-filtered),
  a command console (autocomplete + JSON params), metrics, and **Client
  setup…** with ready-made configs for 15+ clients.

## 4. Connecting clients

### Minimal MCP server (zero dependencies)

```json
{ "mcpServers": { "piterm-qgis": {
    "command": "python",
    "args": ["G:/AIato/pi/piterm-qgis-ai/mcp/piterm_qgis_mcp/minimal.py",
             "--mcp-stdio"] } } }
```

### Full FastMCP server

```json
{ "mcpServers": { "piterm-qgis": {
    "command": "uv",
    "args": ["--directory", "G:/AIato/pi/piterm-qgis-ai", "run", "--no-sync",
             "python", "-m", "piterm_qgis_mcp"] } } }
```

Environment variables understood by both:

| var | default | meaning |
|---|---|---|
| `PITERM_QGIS_HOST` | `127.0.0.1` | bridge host |
| `PITERM_QGIS_PORT` | `9877` | TCP port |
| `PITERM_QGIS_TOKEN` | – | shared secret when auth is enabled |
| `PITERM_QGIS_TIMEOUT` | `60` | per-call seconds |
| `PITERM_QGIS_GZIP` | `0` | `1` → accept gzip responses |
| `PITERM_QGIS_TOOL_MODE` | `granular` | `compound` for clients with tool limits |
| `PITERM_QGIS_TRANSPORT` | `stdio` | `streamable-http` for the full server |

### Direct (no MCP at all)

```bash
python mcp/piterm_qgis_mcp/minimal.py call layers.list '{}'
python mcp/piterm_qgis_mcp/minimal.py screenshot shot.png
curl -s http://127.0.0.1:9879/rpc -H "Content-Type: application/json" \
     -d '{"type":"system.ping"}'
echo '{"type":"layers.list"}' | ncat 127.0.0.1 9877
```

## 5. First-run checklist

1. `system.ping` → `{"pong": true}`.
2. `system.diagnose` → look for `"ok": true` and read the `checks` list.
3. `system.help {"query": "render"}` → confirm the catalog answers.
4. `canvas.render {"path": "<tmp>/map.png"}` → file exists.
5. Decide on security posture (see SECURITY.md) — at minimum generate a token on
   shared machines.

## 6. Updating / uninstalling

* Update: re-run the installer (old version kept as `.bak-*`).
* Reload without restarting QGIS: *Plugins → Piterm QGIS AI → Reload plugin*.
* Uninstall: `install.ps1 -Uninstall` (or `python install.py --uninstall`) —
  removes the plugin folder, sets the ini key to `false`, keeps backups.
  `session.checkpoint_*` files live under the profile's
  `piterm_qgis_ai/checkpoints` and are left in place.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Toolbar button missing | plugin not enabled: *Plugins → Manage → search "Piterm"* |
| `E_BRIDGE_DOWN` from CLI | server not started (dock), wrong port, or QGIS closed |
| Server won't start, "address in use" | another QGIS window/process holds the port; change it in the dock |
| `E_PATH_DENIED` on save | extend `paths.allow` (dock console: `system.config_set`) |
| Everything `E_FORBIDDEN` | read-only mode is on, or execute disabled — see dock toggles |
| Client can't see tools | check the client spawns `python` on PATH; use absolute paths in configs |
| Hermes-style agent breaks with pydantic errors | use the “Hermes” snippet (clears venv env vars in a .bat) |
| *Client setup…* shows "No MCP client script found" | the checkout moved: re-run `install.ps1` (bundles a client copy) or set `meta.repo_root` to the checkout |
| A policy switch will not turn **off** | fixed in 1.0.0 (settings coercion); if you run an older copy, re-run `install.ps1` |
| QGIS log full of “listening failed” | another bridge instance is already bound; stop duplicates |

## 8. Verifying the installation

```powershell
# one command: pytest + smoke + sandboxed coverage + live GUI, per installed build
powershell -ExecutionPolicy Bypass -File scripts\verify_all.ps1 -Sandbox

# quick pass without the heavy sweeps:
scripts\verify_all.ps1 -SkipCoverage -SkipLive
```

Every suite drops JSON evidence into `verification\logs\` and the full console
output of every suite is teed into `verification\logs\verify_all_<stamp>.log`.
`-Sandbox` additionally exercises the two connection-writing commands
(`db.connection_add` / `db.import_layer`) against a throwaway settings root —
operator profiles are never touched (hash-diff evidence included). Individual
suites: `scripts\qgis_headless_smoke.py` (63 checks),
`scripts\qgis_command_coverage.py` (195-command sweep),
`scripts\qgis_live_coverage.py <port>` (33 live checks). See
`verification/report.md` §9–§11 for the current matrix.
