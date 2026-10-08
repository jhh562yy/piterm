# MCP streamable-http transport — end-to-end probe (final-state sweep)

Date: 2026-10-08 23:49 local · contract CT-20261008-01 (post-settlement re-verification)

## Server side

Launcher: `_scratch/mcp_http_launch.py`

```python
mcp = create_server()
mcp.run(transport="streamable-http", host="127.0.0.1", port=18990,
        streamable_http_path="/mcp")
```

Server log:

```
mcp object: MCPServer
ready: streamable-http on 127.0.0.1:18990/mcp
StreamableHTTP session manager started
INFO:     Uvicorn running on http://127.0.0.1:18990
```

## Client side (mcp 2.3 `streamable_http_client` -> `ClientSession`)

```
initialize ok: piterm-qgis / 2025-11-25
tools: 196 | first: canvas_center_set, canvas_extent_get, canvas_extent_set,
       canvas_refresh, canvas_render, canvas_render_3d
system_ping -> {
  "pong": true,
  "time": 1791474580.719276,
  "plugin": "1.0.0"
}
layers_list -> {
  "count": 0,
  "layers": []
}
```

## Binding (loopback only)

```
TCP    127.0.0.1:18990        0.0.0.0:0              LISTENING       9076
```

## Skill CLI re-check

```
python skill/piterm-qgis-ai/piterm_qgis.py ping
  -> {"pong": true, "time": 1791474580.93, "plugin": "1.0.0"}
python skill/piterm-qgis-ai/piterm_qgis.py hello
  -> {"protocol": "piterm-bridge", "protocol_version": 2,
      "plugin_version": "1.0.0", "server": {... "frame_port": 9877,
      "http_port": 9879, ...}}
```
