# Piterm QGIS AI

**Production-grade AI control bridge for QGIS** — a QGIS plugin, an MCP server,
and a zero-dependency CLI/skill, built as one coherent system for the piterm
ecosystem.

Ask any MCP-capable agent (Claude Code / Desktop, Cursor, VS Code, Codex,
Gemini, Qwen, Kimi, Copilot CLI, LM Studio, opencode, Hermes, opencodo…) — or
piterm's own terminal agents — to load data, edit features, run Processing,
style, label, render maps, author layouts, export atlases and checkpoint the
project. Everything runs inside QGIS's own event loop: **no threads touch
PyQGIS**, so nothing randomly crashes.

```
AI agent / skill / curl
   │  MCP stdio (FastMCP or zero-dep minimal)         🆕 both modes, same catalog
   │  HTTP JSON-RPC  http://127.0.0.1:9879/rpc        🆕 CORS + /health + /events
   ▼  TCP frames/NDJSON  127.0.0.1:9877               🆕 4-byte frames AND line mode
┌────────────────────────────────────────────┐
│ QGIS plugin  piterm_qgis_ai                │
│  QTcpServer on the Qt event loop (0 threads)│
│  dispatcher: auth → policy → validate →     │
│              handler → audit/journal        │
│  160+ commands · jobs · checkpoints ·       │
│  journal replay · workflow runner · cursors │
└────────────────────────────────────────────┘
```

## Why this one

Four goals, each with concrete mechanisms (not slogans):

| Goal | What it means here |
|---|---|
| **高性能** | Length-prefixed frames (two `sendall` calls, no copies), optional gzip above 128 KB, NDJSON fast path for shells, connection reuse + `TCP_NODELAY`, batched commands in one round-trip, chunked cursors for huge queries, per-command timing in every response (`meta.t_ms`), zero-thread event loop. |
| **高优化** | One command catalog shared by plugin, MCP server and docs — generated, never drifting. Tool *and* transport modes (granular/compound, stdio/http). Minimal mode needs **zero** third-party packages. Work lands only in the paths you allow. |
| **高支持** | QGIS 3.28 → 4.x (Qt5/Qt6, scoped-enum compat layer), Python 3.9+, Windows/macOS/Linux, 15+ prebuilt client configs, both MCP server flavors, plus a stdlib skill script that needs no MCP at all. |
| **高操控** | Token auth (constant-time, lockout), scopes (`read/write/destructive/execute`), read-only mode, path allow/deny lists, `__confirm` gate, rate limits, full audit trail, session journal with replay export, project checkpoints, dry-run workflow runner, kill/kick, metrics. |

## Repository layout

```
piterm-qgis-ai/
├─ plugin/piterm_qgis_ai/     QGIS plugin (GPL-2.0-or-later)
│  ├─ handlers/               16 modules, 160+ @command methods
│  ├─ server.py               QTcpServer frame+HTTP listeners, reentrancy guard
│  ├─ dispatcher.py           auth → policy → validate → execute → audit
│  ├─ security.py             tokens, scopes, paths, rate limit, audit log
│  ├─ journal.py              mutation journal + replay-script export
│  ├─ checkpoints.py          full-project snapshots & rollback
│  ├─ jobs.py                 background job manager (QgsTask-backed)
│  ├─ cursors.py              chunked reads
│  ├─ ui/                     dock (status/log/console/metrics) + client setup
│  └─ compat.py               QGIS/Qt version shims
├─ mcp/piterm_qgis_mcp/       MCP server + CLI (MIT)
│  ├─ minimal.py              single-file stdlib client + MCP stdio server + CLI
│  ├─ server.py               FastMCP server (dynamic tools from the live catalog)
│  └─ registry.py             tool catalog + compound mode
├─ skill/piterm-qgis-ai/      SKILL.md + piterm_qgis.py (same minimal core)
├─ scripts/                   installer, manifest/doc generator, headless smoke
├─ docs/                      RESEARCH · ARCHITECTURE · PROTOCOL · TOOLS · SECURITY · PERFORMANCE · INSTALL · COMPAT
└─ tests/                     wire/security/manifest/round-trip tests
```

## Quick start (this machine)

```powershell
# 1) deploy into every detected QGIS profile (backup + enable + verify)
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# 2) open QGIS → the π toolbar button starts the bridge
#    (or dock → Start server; 127.0.0.1:9877 TCP, :9879 HTTP)

# 3) smoke check from any shell — stdlib only, no pip:
python mcp\piterm_qgis_mcp\minimal.py ping
python mcp\piterm_qgis_mcp\minimal.py call layers.list '{}'
```

MCP clients: open **Piterm QGIS AI → Client setup…** in QGIS and copy the
pre-filled block for your client. Minimal (stdlib) example:

```json
{ "mcpServers": { "piterm-qgis": {
    "command": "python",
    "args": ["<repo>/mcp/piterm_qgis_mcp/minimal.py", "--mcp-stdio"] } } }
```

Full FastMCP mode (richer schemas, resources, prompts) — works with both
`mcp` 1.x (`FastMCP`) and 2.x (`MCPServer`); the server imports whichever is
installed, or falls back to the zero-dependency minimal server:

```bash
uv run --directory <repo> python -m piterm_qgis_mcp serve
```

## Command surface

160+ commands across `system` · `project` · `layers` · `features` · `raster` ·
`processing` · `job` · `canvas` · `style` · `layout` · `session` · `io` ·
`db` · `plugins` · `expression` · `python` · `ui` ·
see **[docs/TOOLS.md](docs/TOOLS.md)** (generated from the live registry) or ask
the bridge itself:

```bash
python mcp/piterm_qgis_mcp/minimal.py call system.help '{"query":"render"}'
```

High-level capabilities: sync + background Processing with progress/cancel,
raster calculator & zonal stats, categorized/graduated styling, labeling,
QML import/export, print layouts & atlas export (PDF/PNG/SVG), spatial queries,
field calculator, cursors, SQL (with a read-by-default gate), checkpoints,
journal replay, multi-step workflows with `$N.field` references and dry-run.

## Security posture

Localhost-only by default; binding a non-loopback address **requires** a token.
Read-only mode, path allow-lists, `__confirm` for destructive ops, per-client
rate limits, audit JSONL, and a sandboxed `python.exec` (qgis/math imports only,
no eval/open/dunder escape hatches) alongside a deliberately gated
`python.exec_full`. See [docs/SECURITY.md](docs/SECURITY.md).

## piterm integration

- Zero-dependency CLI (`piterm-qgis` or `python minimal.py`) speaks the same
  protocol piterm's shell pipelines already use (NDJSON one-liners work).
- The MCP stdio server plugs straight into piterm's Node bridge
  (`asmcore/tools/pi_mcp_bridge.js` pattern) or any MCP client.
- `PiterM_QGIS_*` env vars keep host/port/token configurable per session.

## Testing

```bash
# unit tests (no QGIS needed)
.venv/Scripts/python -m pytest tests -q

# headless end-to-end on a real QGIS install
"C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat" scripts/qgis_headless_smoke.py
"C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat"       scripts/qgis_headless_smoke.py
```

## License

MIT for the MCP server / CLI / skill; GPL-2.0-or-later for the QGIS plugin
(matching the QGIS plugin ecosystem). See [LICENSE](LICENSE).
