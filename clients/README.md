# Client configuration samples

The QGIS dock's **Client setup…** dialog generates these snippets from your live
settings (host, ports, token, repo path) and can write them into the real config
files (with a `.bak` backup, merging JSON instead of overwriting). The files in
this folder are static samples for reference.

| Client | Config file | Root key |
|---|---|---|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` | `mcpServers` |
| Claude Code | `claude mcp add ...` (see dialog) | — |
| Cursor | `~/.cursor/mcp.json` | `mcpServers` |
| VS Code | `<project>/.vscode/mcp.json` | `mcpServers` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |
| Gemini CLI | `~/.gemini/settings.json` | `mcpServers` |
| Qwen Code | `~/.qwen/settings.json` | `mcpServers` |
| Kimi Code CLI | `~/.kimi-code/mcp.json` | `mcpServers` |
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` | `mcpServers` |
| LM Studio | `~/.lmstudio/mcp.json` | `mcpServers` |
| opencode | `%APPDATA%\opencode\config.json` | `mcp` |
| Codex CLI | `~/.codex/config.toml` | `[mcp_servers.*]` |
| Hermes | `%APPDATA%\Hermes\config.yaml` (+ .bat launcher) | `mcpServers` |

Two ways to run the server:

* **minimal (stdlib)** — `python <repo>/mcp/piterm_qgis_mcp/minimal.py --mcp-stdio`
* **full (FastMCP)** — `uv --directory <repo> run --no-sync python -m piterm_qgis_mcp`

Environment variables: `PITERM_QGIS_HOST`, `PITERM_QGIS_PORT`,
`PITERM_QGIS_TOKEN`, `PITERM_QGIS_TIMEOUT`, `PITERM_QGIS_GZIP`,
`PITERM_QGIS_TOOL_MODE`.
