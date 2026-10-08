# Security & control model

The bridge is a **local automation surface**: whoever can reach the port can
drive QGIS. The model below assumes the honest case (you and your agents) while
raising real costs for the dishonest ones (other local processes, careless
agents, stale configs).

## 1. Threat model

| Actor | Capability | Mitigation |
|---|---|---|
| Another user's process on a shared machine | cannot read your files, can often reach loopback ports | token auth |
| Same-user rogue process / script | can read your config and env | *not* preventable locally (documented honestly); audit trail + lockout raise the cost |
| A misbehaving agent | will `rm -rf` your layer if allowed | scopes, confirm gate, read-only mode, journal/checkpoint rollback |
| A stale client config pointing at the wrong host | accidental writes | manifest/capabilities handshake + `diagnose`; non-loopback requires token |
| Network (LAN/WAN) | reach the port if bound to 0.0.0.0 | **binding non-loopback without a token is refused at start** |

## 2. Controls

### 2.1 Binding
* Default `127.0.0.1` only. `localhost`, `::1` are treated as loopback too.
* `server.start()` refuses any non-loopback host unless `auth.enabled` **and** a
  non-empty token are configured, and says so in the dock and in
  `system.diagnose`.

### 2.2 Token
* `auth.enabled` + `auth.token`; compared with `hmac.compare_digest`.
* 5 consecutive failures on one connection → connection dropped
  (`E_AUTH_LOCKED`), failures counted per connection.
* Token travels in the message (`token`) over loopback, or via
  `X-Piterm-Token` / `Authorization: Bearer` on HTTP.
* Generate one from the dock ("Generate"), distribute it via the client
  configurator; `PITERM_QGIS_TOKEN` env var for CLI users.

### 2.3 Scopes

| Scope | Grants | Disabled by |
|---|---|---|
| `read` | inspection, queries, renders, navigation | — (always on) |
| `write` | layers, features, styles, project edits | `mode.read_only=true` |
| `destructive` | delete/remove/overwrite/import-to-db | 同上 |
| `execute` | processing algorithms, `python.exec` | `mode.allow_execute=false` |

Every command declares its risk (`read < write < destructive < execute`) and the
dispatcher rejects anything above the granted scopes with `E_FORBIDDEN` and a
hint at the setting to flip.

### 2.4 Confirmation gate
`mode.confirm_destructive=true` makes `destructive` commands require an explicit
`"__confirm": true` parameter (`E_CONFIRM_REQUIRED` otherwise). MCP clients also
see tool annotations (`readOnlyHint`/`destructiveHint`) so their own approval UI
can prompt first. Both layers are independent on purpose.

### 2.5 Path allow/deny lists
Every file-touching command (`io.*`, exports, project save/open, downloads)
runs through `SecurityManager.check_path`:

* `paths.allow` (default `~`, `:TEMP:`, `:PROJECT:`) — prefix match after
  expanduser/expandvars/abspath-normalization;
* `paths.deny` — checked first, wins over allow; use it to fence off
  `.ssh`, credential stores, production shares;
* `:TEMP:` and `:PROJECT:` resolve dynamically (project dir = current `.qgz`
  location), so agents get a scratch area without a blanket `*`.
* Violations: `E_PATH_DENIED`, listing the active patterns.

### 2.6 Code execution
Two commands, deliberately different:

| | `python.exec` (default) | `python.exec_full` |
|---|---|---|
| imports | `qgis`, `math` only (top-level match) | unrestricted |
| calls blocked | `eval/exec/compile/open/input/__import__/getattr/setattr/type/object/super/globals/locals`… | none |
| dunder access | blocked | allowed |
| builtins | 23-name whitelist | full |
| extra gate | scope `execute` | scope `execute` **+** `dangerous.allow_full_python` |
| timeout | wall-clock trace hook (30 s default) | same |
| output | stdout captured, restored on every path | same |

Both report `E_EXEC` with `detail.stdout`/`detail.traceback` when user code
raises, so the caller sees what happened.

### 2.7 Rate limiting & client caps
`limits.rate_per_sec` token-bucket per connection (`E_BUSY`); at most
`MAX_CLIENTS=16` concurrent connections, `LISTEN_BACKLOG` for the rest.
`system.clients_kick` disconnects a misbehaving client.

### 2.8 Audit & forensics
* **Audit log**: every command (minus ping/hello/help) → ring buffer +
  JSONL at `…/profile/piterm_qgis_ai/audit/audit.jsonl`, 8 MB rotation,
  `system.audit_tail`.
* **Journal**: every mutating command with redacted params →
  `session.journal_list` / `session.journal_export` (JSONL or replay script).
* **Checkpoints**: `session.checkpoint_create` before risky work;
  `session.checkpoint_restore` rewinds the project (memory layers included).
* **Message ring**: the QGIS message log (last 1000 entries) is mirrored inside
  the plugin → `system.messages`, visible in the dock.

## 3. Honest limits (what this does NOT do)

* It cannot stop a **same-user** process from reading your token or talking to
  the bridge — no local scheme can. It bounds impact: scopes, confirm, audit,
  rollback.
* `python.exec_full` is arbitrary code by design; treat enabling it as granting
  shell on the QGIS process (and therefore your user account).
* The HTTP endpoint supports CORS `*` for convenience; while the server binds
  loopback that is fine for local tools, but do not combine `CORS *` with a
  non-loopback bind unless you also trust every page the browser loads. (Token
  still applies there.)
* A malicious *plugin* loaded into QGIS is outside this model entirely.

## 4. Recommended configurations

**Interactive power user (defaults + token):**
```ini
auth.enabled=true            auth.token=<generated>
mode.read_only=false         mode.allow_execute=true
mode.confirm_destructive=false
dangerous.allow_full_python=false
```

**Unattended agent / shared machine:**
```ini
mode.read_only=true          # flip to false only for planned edit runs
mode.allow_execute=false     # use explicit allow per session
mode.confirm_destructive=true
paths.allow=:PROJECT:;:TEMP: # no home-dir access at all
limits.rate_per_sec=20
```

**Data-quality review only:** `mode.read_only=true`, keep execute off, rely on
`render`/`query`/`stats`.

## 5. Incident response

1. `system.clients` → `system.clients_kick <id>` (or `system.shutdown`).
2. `system.audit_tail 500` — see exactly what ran.
3. `session.journal_list` → `session.journal_export` for a timeline.
4. `session.checkpoint_list` → `session.checkpoint_restore <id>` if project state
   was damaged.
5. Rotate the token; re-check `paths.allow`; consider `mode.read_only=true`
   until the cause is understood.
