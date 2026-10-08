# Architecture

## 1. Components

```
┌──────────────────────────────┐        ┌───────────────────────────────────────┐
│  MCP clients / agents        │        │  QGIS (GUI process)                   │
│  Claude, Cursor, VS Code,    │        │  ┌─────────────────────────────────┐  │
│  Codex, Gemini, Qwen, Kimi,  │        │  │ plugin: piterm_qgis_ai          │  │
│  Copilot, LM Studio, Hermes, │◄──────►│  │  QTcpServer (frame) :9877       │  │
│  opencode, piterm terminal   │  TCP/  │  │  QTcpServer (http)  :9879       │  │
└──────────┬───────────────────┘  HTTP  │  │  Dispatcher ─→ HandlerHub       │  │
           │                            │  │   160+ @command methods         │  │
           │ stdio / streamable-http    │  │  SecurityManager · Journal      │  │
┌──────────▼───────────────────┐        │  │  Checkpoints · JobManager       │  │
│ mcp/piterm_qgis_mcp          │        │  │  CursorManager · AuditLog       │  │
│  ▲ dynamic tools from live   │        │  └──────────────┬──────────────────┘  │
│    catalog (no drift)        │        │                 │ PyQGIS (GUI thread)  │
│  ▲ minimal.py: stdlib only   │        │  QgsProject · QgsVectorLayer · …      │
│  ▲ server.py: FastMCP full   │        └───────────────────────────────────────┘
└──────────────────────────────┘
           ▲
           │ python piterm_qgis.py call …
┌──────────┴───────────────────┐
│ skill/piterm-qgis-ai         │  zero-dependency CLI + SKILL.md
└──────────────────────────────┘
```

## 2. Threading model (the most important invariant)

**No plugin code ever touches PyQGIS off the GUI thread.** That is achieved by
running *everything* on Qt's own event loop:

* `QTcpServer`/`QTcpSocket` are signal-driven; there is no accept loop, no
  worker thread, no busy polling;
* long operations never run inline: they are handed to `QgsTask`
  (`processing.start`) or to the job manager, which reports progress into plain
  Python objects; the socket loop stays responsive the whole time;
* the one place a long call *does* run inline (`processing.execute`,
  `canvas.render`) pumps the event loop through a `ResponsiveFeedback` /
  `QEventLoop` wait, and the reentrancy guard (below) keeps that safe;
* if a future handler ever needs to call in from another thread, the documented
  pattern is GeoAgent's `run_on_qt_gui_thread` (BlockingQueuedConnection +
  `moveToThread` back in `finally`, thread comparison by equality) — kept in
  docs/RESEARCH.md as the sanctioned approach.

### Reentrancy guard

Pumping the event loop means `readyRead` can fire *inside* a running handler.
`PitermServer.process_payload()` therefore queues any message that arrives while
`_in_dispatch` is true and drains the queue only after the current command
finishes. Without this, a second client's command could interleave with the
first one's edit session and corrupt project state.

## 3. Dispatch pipeline

```
socket bytes ─decoder→ message ─→ Dispatcher.handle_message
  ├─ absorb meta (client, gzip, subscriptions)
  ├─ auth            (constant-time token check, failure lockout per connection)
  ├─ rate limit      (token bucket, optional)
  ├─ registry lookup (unknown → difflib suggestions)
  ├─ policy          (read-only, execute gate, deny-list, __confirm)
  ├─ param validation(signature-checked: unknown/missing params → E_BAD_REQUEST)
  ├─ handler         (Hub method; CommandError → structured error)
  ├─ timing          (meta.t_ms; system.metrics aggregates per command)
  └─ records         (journal for mutations, audit for everything, UI event)
```

Handlers never see sockets; sockets never see PyQGIS. That boundary is what
makes both sides unit-testable.

## 4. Module map

| Module | Responsibility |
|---|---|
| `wire.py` | frame/text decoding, gzip, JSON safety, limits |
| `registry.py` | `@command` decorator + spec store (name, risk, tags, params) |
| `dispatcher.py` | pipeline above + events + capabilities payload |
| `server.py` | QTcpServer listeners, HTTP parsing, connection contexts, reentrancy guard |
| `security.py` | tokens, scopes, path allow/deny, rate limit, audit log |
| `journal.py` | mutation journal + replay script export |
| `checkpoints.py` | project snapshots + guarded restore |
| `jobs.py` | job state machine (QGIS-free), progress/events |
| `cursors.py` | chunked-read cursors with TTL |
| `handlers/*` | 16 domain modules, 160+ commands |
| `ui/dock.py` | status/log/console/metrics/clients, live settings |
| `ui/configurator.py` | 15+ ready-made client config snippets |
| `compat.py` | QGIS 3/4 + Qt5/6 resolution (`resolve()` over scoped enums) |
| `mcp/minimal.py` | stdlib client + MCP stdio server + CLI (one file) |
| `mcp/server.py` | FastMCP server, dynamic tools/resources/prompts |

## 5. Editing semantics

`HandlerBase.edit_scope(layer, description)` wraps every write:

* no open edit session → `startEditing()` + edit macro + `commitChanges()` on
  success, `rollback()` on failure (the response says whether writes were
  buffered by a pre-existing session);
* an open session (user or `session.edit_begin`) → the command joins it,
  produces an undoable macro, and **does not commit** — the session owner
  decides;
* macro failures call `destroyEditCommand()` so no broken half-macro is left on
  the undo stack.

## 6. Jobs

`JobManager` is deliberately QGIS-free (testable): states
`queued → running → succeeded|failed|cancelled`, progress 0-100, message, log
ring, result/error. Handlers bridge real work to it
(`QgsProcessingAlgRunnerTask` for algorithms, exporters for layouts). Every state
change emits an event (`job.*`) to subscribed connections _and_ is pollable via
`job.get`. Cancellation is cooperative (`task.cancel()`), and finished jobs are
pruned to a bounded list.

## 7. Sessions, journal, checkpoints

* **Journal**: every non-read command lands with trimmed params (secrets and
  code redacted). `session.journal_export` writes either JSONL or a standalone
  Python replay script that only needs the stdlib.
* **Checkpoints**: `QgsProject.write()` into the plugin storage dir (memory
  layers included), index file, max 31 with LRU pruning. Restore goes through
  `compat.read_project_guarded()` which auto-dismisses the modal "Handle
  Unavailable Layers" dialog that would otherwise freeze the socket forever.
* **Undo**: layers keep their own undo stacks; `session.undo/redo` drive them,
  and every command-level macro is visible there.

## 8. Compatibility strategy

`compat.resolve(obj, "LayerType.VectorLayer", "VectorLayer")` resolves scoped
(QGIS 4) and flat (QGIS 3) enums in one call; all version conditionals live in
one file. Message levels, field types, render flags, Qt connection enums and
`QgsRasterCalculator`'s home module (`qgis.analysis` in 3.x, `qgis.core` in
4.x) are all resolved the same way. Code avoids runtime PEP 604 unions (QGIS
3.28+ still binds Python 3.9, where `isinstance(x, A | B)` raises).

## 9. Extending: adding a command

1. Write a method in the right `handlers/*.py` module and decorate it:

```python
@command(name="layers.shout", summary="Example", risk="write", tags=("layers",),
         returns="…")
def layers_shout(self, layer, text="hi"):
    target = self.resolve_vector({"layer": layer})
    compat.push_message(text)
    return {"layer": target.id()}
```

2. That's it — the dispatcher validates params from the signature, the dock
   console gets autocomplete, `system.help`/`capabilities` include it, the MCP
   server exposes it automatically (next start), `docs/TOOLS.md` regenerates,
   and `tests/test_manifest.py` asserts the counts kept moving.
