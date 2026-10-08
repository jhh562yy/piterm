# Protocol reference — Piterm Bridge v2

One dispatcher, three transports. Whatever the transport, a *message* is a JSON
object and the *answer* is a JSON object.

```
TCP 127.0.0.1:9877   frame: 4-byte BE length + JSON (optional gzip body)
                     text:  one JSON object per line (NDJSON)
HTTP 127.0.0.1:9879  POST /rpc   (same JSON object as a request body)
                     GET  /health | /info | /commands | /events?since=&limit=
```

## 1. Envelope

Request:

```json
{
  "id": 42,                          // any JSON scalar; echoed back
  "type": "layers.list",             // command name (see docs/TOOLS.md)
  "params": {"kind": "vector"},      // command parameters (object)
  "token": "…",                     // only when auth is enabled
  "meta": {
    "client": "claude-code",         // free-form client name (shown in the dock)
    "version": "1.2.3",              // client version (diagnose uses it)
    "gzip": true,                    // accept gzip-compressed responses
    "mode": "frame",                 // optional for HTTP; frame|text
    "subscribe": ["job", "log"]      // push events for these prefixes
  }
}
```

Response:

```json
{"id": 42, "ok": true,  "result": { … }, "meta": {"cmd": "layers.list", "t_ms": 3.14}}
{"id": 42, "ok": false, "error": {"code": "E_NOT_FOUND",
                                  "message": "no layer matching 'roadz'",
                                  "detail": {"available": ["roads", "rails"]}},
                        "meta": {"cmd": "layers.list", "t_ms": 1.02}}
```

Push event (only for subscribed connections, between responses):

```json
{"seq": 17, "event": "job.progress", "data": {"job_id": "job-3", "progress": 42,
                                              "state": "running"}}
```

## 2. Framing details

* **Frame mode**: header = `struct.pack(">I", len(body))`; body = UTF-8 JSON or
  gzip (magic `1f 8b` auto-detected). Inbound cap 64 MiB; a bad length is fatal
  for that connection (reported and closed); a bad *body* is answered with
  `E_PARSE` and the connection stays alive.
* **Text mode**: auto-selected when the first byte is `{`. One object per `\n`.
  Responses mirror the request mode. Perfect for
  `echo '{"type":"system.ping"}' | ncat 127.0.0.1 9877`.
* Responses over 128 KiB are gzip-compressed when the client set `meta.gzip`.
* Backpressure: if a connection's unsent bytes exceed 256 MiB it is dropped
  (stalled peer protection).

## 3. Handshake & discovery

`system.hello` (or `system.ping` for the cheap check) returns:

```json
{
  "protocol": "piterm-bridge", "protocol_version": 2,
  "plugin_version": "1.0.0",
  "server": {"running": true, "frame_port": 9877, "http_port": 9879,
             "clients": 1, "stats": {…}},
  "security": {"auth_enabled": false, "read_only": false, "allow_execute": true,
               "scopes": ["read","write","destructive","execute"]},
  "limits": {"max_message": 67108864, "max_clients": 16},
  "commands": {"count": 163, "names": ["system.ping", …]},
  "features": ["frame","text","gzip","batch","events","jobs","cursors",
               "checkpoints","journal","http","workflow","audit"]
}
```

`system.capabilities` returns the full catalog **with parameters** — this is what
the MCP server uses to build tool schemas at runtime, so tool lists can never
drift from the plugin.

## 4. Batch & workflows

`system.batch` runs many commands in one round-trip:

```json
{"type":"system.batch","params":{"stop_on_error":false,"commands":[
  {"type":"layers.count","params":{"layer":"roads"}},
  {"type":"features.query","params":{"layer":"roads","limit":3}}
]}}
```
→ `{"count":2,"ok_count":2,"results":[{"index":0,"type":"…","ok":true,"result":{…}}, …]}`

Every inner command goes through the same policy checks and its own audit entry.
`system.batch` and `system.shutdown` cannot be nested.

`session.workflow_run` is the scripting layer on top: steps carry `$N.field`
references into earlier results (`$0.result.layer.id`), `dry_run:true` performs
policy validation only, `stop_on_error` decides whether to continue.

## 5. Events & jobs

Long operations (`processing.start`, exports…) return a job id immediately:

```json
{"type":"processing.start","params":{"algorithm":"native:buffer","parameters":{…}}}
→ {"job":"job-7","state":"queued"}
{"type":"job.get","params":{"id":"job-7","with_log":true}}
→ {"id":"job-7","kind":"processing","state":"running","progress":37,
   "message":"…","log":["12:00:01 …"]}
```

Subscribe (`meta.subscribe: ["job"]`) to receive `job.progress` / `job.succeeded`
/ `job.failed` / `job.log` events as they happen; without a subscription use
`system.events_poll {"since": <seq>}`.

## 6. Cursors (chunked reads)

```json
{"type":"features.cursor_open","params":{"layer":"big","filter":"\"v\">0"}}
→ {"cursor":"cur-1","kind":"features","meta":{…}}
{"type":"features.cursor_fetch","params":{"cursor":"cur-1","n":500}}
→ {"cursor":"cur-1","items":[ … ],"done":false,"fetched":500}
```
Cursors expire after 15 min idle; at most 32 open at once.

## 7. Error codes

| code | meaning | typical fix |
|---|---|---|
| `E_BAD_REQUEST` | envelope/params malformed (detail lists accepted params) | fix typo |
| `E_AUTH` / `E_AUTH_LOCKED` | token missing/wrong; too many failures | set token |
| `E_FORBIDDEN` | read-only mode, execute disabled, command deny-list | adjust settings |
| `E_CONFIRM_REQUIRED` | destructive op needs `"__confirm": true` | confirm |
| `E_PATH_DENIED` | path outside allow-list / inside deny-list | extend `paths.allow` |
| `E_NOT_FOUND` | layer/field/layout/job missing (detail has candidates) | use `layers.find` |
| `E_AMBIGUOUS` | name matches several layers | pass the layer id |
| `E_UNKNOWN_COMMAND` | plus `suggestions` from difflib | `system.help` |
| `E_EXPRESSION` | QGIS expression parse/prepare error | `expression.validate` |
| `E_GEOMETRY` / `E_CRS` | geometry/CRS not parseable | pass WKT/WKB, `EPSG:xxxx` |
| `E_STATE` | wrong state (not editable, empty selection…) | fix the call order |
| `E_TIMEOUT` | plugin-side deadline (55 s default) hit | use `processing.start` |
| `E_BUSY` | rate limit / max clients | retry later |
| `E_EXEC` | sandboxed code raised / sandbox violation | read `detail.stdout` |
| `E_INTERNAL` | unexpected — `detail.traceback` has the traceback | report |

## 8. HTTP transport

| endpoint | method | purpose |
|---|---|---|
| `/rpc` | POST | one message (same JSON as TCP). Auth via `X-Piterm-Token` header or `?token=`. |
| `/health` | GET | `{"ok":true,"service":"piterm-qgis","time":…}` |
| `/info` | GET | the `system.hello` payload |
| `/commands` | GET | command names |
| `/events?since=0&limit=200` | GET | event poll (same as `system.events_poll`) |

CORS is open (`Access-Control-Allow-Origin: *`) so browser tools can call it.
`OPTIONS` returns 204. Keep-alive supported; `Connection: close` honored.

```bash
curl -s http://127.0.0.1:9879/rpc -H "Content-Type: application/json" \
     -d '{"type":"system.ping"}'
echo '{"type":"layers.list"}' | ncat 127.0.0.1 9877
```

## 9. Versioning & timeouts

* Protocol version is negotiated through `hello.protocol_version` (currently 2).
  Additive fields are backward-compatible; a client that sees a lower
  `plugin_version` should call `system.help` before using new commands.
* Plugin-side synchronous budget: **55 s** (deliberately below the common 60 s
  client timeout, so QGIS reports the reason instead of a client giving up on a
  still-running algorithm). `python.exec` default: 30 s. Long work: jobs.
* `meta.t_ms` on every response makes latency visible to agents; `system.metrics`
  aggregates per-command timings.
