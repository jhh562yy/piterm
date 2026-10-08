# Compatibility matrix & strategy

## 1. Verified builds (evidence in `verification/logs/`)

| QGIS | Qt | Python | Status |
|---|---|---|---|
| **3.44.15 LTR** | 5.15.13 | 3.12.14 | smoke 63/63 + coverage sweep incl. sandboxed db (177 pass / 0 hard fail) + deploy verified |
| **4.2.3** | Qt 6 | 3.12 | smoke 63/63 + coverage sweep incl. sandboxed db (177 pass / 0 hard fail) verified |
| **4.3.0-dev** | Qt 6 | 3.12 | smoke 63/63 + coverage sweep incl. sandboxed db (177 pass / 0 hard fail) verified |
| 4.2.0 | Qt 6 | — | not a runnable build: uninstall leftover (`bin\setup.bat` + `etc\preremove-conf.bat` only) — excluded from the matrix |
| 3.28–3.40 | 5.15 | 3.9–3.11 | declared supported; compat layer written for these (see below) |

Declared in `metadata.txt`: `qgisMinimumVersion=3.28`, `qgisMaximumVersion=4.99`,
`supportsQt6=True`. Coverage evidence: `verification/report.md` §9–§10; the
sandbox runs write zero bytes to operator profiles (hash diff in
`verification/logs/user_profile_hash_*.json`).

## 2. The compat layer (`plugin/piterm_qgis_ai/compat.py`)

QGIS 4 moved most enums to scoped forms. Instead of scattering version checks,
one resolver handles both spellings:

```python
compat.resolve(QgsMapLayer, "LayerType.VectorLayer", "VectorLayer")
compat.resolve(Qgis, "MessageLevel.Warning", "Warning")
compat.resolve(QgsMapSettings, "RenderFlag.Antialiasing", "Antialiasing")
```

Resolved centrally today:

| Concern | QGIS 3 | QGIS 4 | Handling |
|---|---|---|---|
| message levels | `Qgis.Warning` | `Qgis.MessageLevel.Warning` | `resolve()` |
| layer types | `QgsMapLayer.VectorLayer` | `LayerType.VectorLayer` | `layer_kind()` |
| field types | `QVariant.Int` | `QMetaType.Type.Int` | `field_type_enum()` tries QMetaType → QVariant |
| selection behavior | `QgsVectorLayer.SetSelection` | `SelectionBehavior.SetSelection` | `_selection_behavior()` |
| geometry enums | `QgsWkbTypes.PointGeometry` | `QgsWkbTypes.GeometryType.PointGeometry` | same-class comparison, no cross-class casts |
| `QgsRasterCalculator` | `qgis.analysis` | `qgis.core` | import fallback in `raster.py` |
| raster stats enum | `QgsRasterBandStats.All` | `Statistic.All` | `_stats_enum()` |
| gradient enum | `QgsGraduatedSymbolRenderer.Jenks` | `Mode.Jenks` | `_graduation_mode()` |
| Qt enums | flat (`Qt.AlignCenter`) | nested (`AlignmentFlag.AlignCenter`) | `resolve()` at every use site |
| message log signal | `messageReceived` | `messageReceivedWithFormat` | plugin tries format-signal first |
| join info class | `QgsVectorJoinInfo` | `QgsVectorLayerJoinInfo` | import fallback |
| layout export settings | nested classes | same nested, extra fields | `resolve()` + guarded attribute set |

## 3. Python-version rules (enforced by convention)

* Target **3.9+** syntax (QGIS 3.28 ships 3.9): no `match`, no runtime
  `X | Y` unions — in `isinstance`/`cast` use tuples or `Optional[...]`.
  Annotations may use modern syntax thanks to
  `from __future__ import annotations` (never evaluated at runtime).
* Stdlib-only core: plugin core modules (`wire`, `registry`, `security`,
  `journal`, `jobs`, `cursors`, `constants`, `errors`) and the whole
  `minimal.py` import cleanly outside QGIS (the unit tests do exactly that).
* No dependence on `orjson`/`ujson` — one less thing to break inside QGIS's
  embedded interpreter.

## 4. Port & service behaviour

* Loopback defaults (9877/9879) avoid the classic 9876 of other QGIS MCP
  bridges, so both can coexist (useful for comparison testing).
* On Windows we rely on `QTcpServer`, which owns the socket options; the
  `SO_EXCLUSIVEADDRUSE` subtlety that raw-socket bridges must handle manually
  does not apply.
* Non-loopback binds are refused unless a token is configured (see SECURITY).

## 5. Client compatibility

Any MCP stdio client works with `minimal.py`; the configurator ships snippets
for: Claude Desktop, Claude Code, Cursor, VS Code, Windsurf, Zed-ready generic
JSON, Gemini CLI, Qwen Code, Kimi Code CLI, GitHub Copilot CLI, LM Studio,
opencode, Codex CLI (TOML), Hermes desktop (bat + YAML), and piterm's terminal.
The full server additionally supports `streamable-http` when a client prefers
HTTP over stdio.

MCP SDK coverage: `mcp` **2.x** (`mcp.server.mcpserver.MCPServer`) and
**1.x** (`mcp.server.fastmcp.FastMCP`) are both supported - the server picks
whichever is installed - and the minimal stdio server needs no SDK at all, so
the bridge keeps working even in a bare Python environment.

## 6. Adding a new compat shim

1. Add the resolver call in `compat.py` (never inline a version check in a
   handler — reviewers grep for `version_int()` outside compat and reject it).
2. If the API itself changed shape (not just enum nesting), write a small
   helper function in compat and use it.
3. Extend `tests/test_manifest.py`'s import smoke or the headless script if the
   change affects a command.
