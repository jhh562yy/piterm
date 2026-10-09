# Piterm QGIS AI 使用手册（中文）

从安装 → 启动 → 接入 → 调用 → 安全 → 验证的完整操作说明。文中命令可直接复制，
路径为本机实际路径（`G:\AIato\pi\piterm-qgis-ai`），换成你自己的即可——插件本身不写死任何路径。

> 时间紧：看 §1（60 秒上手）和 §6（配方）就能干活；其余是"出问题怎么办 / 要更严怎么配"。

---

## 0. 先建立心智模型（读一遍）

只有三个部件，其中只有一个在监听端口：

```
  你的 AI 客户端 / 终端 / 脚本
        │
        │  ① CLI + 技能脚本（零依赖，无需 MCP、无需 pip）
        ├─ ② MCP stdio（极简版或 FastMCP 版，同一条命令目录）
        │  ③ 直连协议（TCP 9877 帧/NDJSON ｜ HTTP 9879 JSON-RPC）
        ▼  127.0.0.1:9877               127.0.0.1:9879
  ┌─────────────────────────────────────────────────────────────┐
  │ QGIS 进程（唯一允许跑 PyQGIS 的地方）                        │
  │  插件 piterm_qgis_ai                                        │
  │   QTcpServer 跑在 Qt 事件循环上 —— 全程 0 线程                │
  │   分发链：auth → policy → validate → handler → audit         │
  │   195 条命令 · 后台作业 · 游标 · 检查点 · 日志账本            │
  └─────────────────────────────────────────────────────────────┘
```

三个必须记住的推论：

1. **QGIS 必须开着、桥必须启动**，否则一切返回 `E_BRIDGE_DOWN`。
2. **桥就是一个本地自动化面**：能连上端口的人就能操控 QGIS。默认只绑回环（本机）；
   需要更强控制时用 token / 只读 / 确认门 / 路径白名单（§7）。
3. **命令目录是生成的**，文档与实现不会漂移：`docs/TOOLS.md`、
   `mcp/piterm_qgis_mcp/manifest.json`、`system.capabilities` 同源。随时问桥自己：
   `system.help {"query": "..."}`。

---

## 1. 60 秒上手

```powershell
cd G:\AIato\pi\piterm-qgis-ai

# 1 · 部署到所有检测到的 QGIS 配置（自动备份 / 启用 / 内置客户端副本）
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -All

# 2 · 打开 QGIS → 点工具栏 π 按钮（或 Dock → "Start server"）
#     状态点变绿：running · frame 9877 / http 9879 · 0 client(s)

# 3 · 任意终端里验证（只用标准库，不需要 pip / MCP）
python mcp\piterm_qgis_mcp\minimal.py ping
python mcp\piterm_qgis_mcp\minimal.py call system.diagnose '{}'
python mcp\piterm_qgis_mcp\minimal.py screenshot shot.png
```

预期：`{"pong": true, "plugin": "1.0.0"}`、`diagnose` 里 `"ok": true`、磁盘上出现 PNG。
任何一步不符合预期，直接跳到 §9。

---

## 2. 安装插件

### 自动（Windows）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1          # 仅 default 配置
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -All     # 所有配置
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Profile default,custom
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoEnable    # 只拷文件不改 ini
```

每个配置文件夹会做这些事：

1. 已存在的 `piterm_qgis_ai` 先改名成 `.bak-<时间戳>`（**不会删除**你的东西）；
2. 拷入插件（剔除 `__pycache__`）和生成的 `manifest.json`；
3. **内置零依赖客户端**到 `<插件>\mcp\piterm_qgis_mcp\minimal.py`，
   这样即使仓库搬走了，"Client setup…"依然可用；
4. 先在 ini 里记下仓库位置（`[piterm_qgis_ai]` 段 `meta.repo_root=…`，改 ini 前先备份），
   再把 `piterm_qgis_ai=true` 写进 `[PythonPlugins]`。

### 自动（Linux/macOS/CI）

```bash
python install.py [--all-profiles] [--profile default] [--uninstall] [--list]
```

### 手动

1. 把 `plugin/piterm_qgis_ai/` 拷进配置目录的 `python/plugins/`
   （QGIS：*设置 → 用户配置 → 打开当前配置文件夹*）；可选把
   `mcp/piterm_qgis_mcp/minimal.py` 拷到 `<插件>/mcp/piterm_qgis_mcp/`。
2. 重启 QGIS → *插件 → 管理 → 勾选 "Piterm QGIS AI"*。
3. 点工具栏 π 按钮，或打开 Dock 点 **Start server**。

环境要求：QGIS **3.28+**（已在 3.44.15 LTR、4.2.3、4.3.0-dev 验证）；
CLI/MCP 端 Python **3.9+**（只用标准库）。`mcp>=1.0` 是可选项，仅 FastMCP 模式需要。

---

## 3. 启动桥

| | |
|---|---|
| **工具栏** | π 按钮，开/关 |
| **Dock** | *插件 → Piterm QGIS AI*：状态点、客户端列表、日志、命令台、指标 |
| **端口** | 帧/NDJSON **9877**，HTTP **9879**（Dock 里改，会持久化） |
| **自启** | Dock 勾选 *Auto-start with QGIS*（`mode.autostart=true`） |
| **绑定** | 默认 `127.0.0.1`；非回环地址**必须**先设 token，否则拒绝启动 |

Dock 里还能：踢客户端、按级别过滤实时日志、内置命令台直接发命令（带补全 + JSON 参数）、
看每条命令的耗时统计、打开 **Client setup…**。

不依赖任何客户端库的健康检查：

```powershell
python mcp\piterm_qgis_mcp\minimal.py --port 9877 ping      # TCP 帧/NDJSON
curl -s http://127.0.0.1:9879/health                        # HTTP
curl -s http://127.0.0.1:9879/info                          # 协议与能力位
```

两个端口都属于 QGIS 进程：QGIS 一关，两个都没了——那是 `E_BRIDGE_DOWN`，不是 bug。

---

## 4. 首次自检清单

```powershell
python mcp\piterm_qgis_mcp\minimal.py call system.ping '{}'          # {"pong": true}
python mcp\piterm_qgis_mcp\minimal.py call system.diagnose '{}'      # "ok": true + checks[]
python mcp\piterm_qgis_mcp\minimal.py call system.help '{"query":"render"}'
python mcp\piterm_qgis_mcp\minimal.py call layers.count '{}'         # 空工程为 0
python mcp\piterm_qgis_mcp\minimal.py call canvas.render '{"path":"G:/AIato/pi/_scratch/map.png"}'
```

`system.diagnose` 是最好的"一键体检"：版本、端口、客户端数、每条命令的计数、
安全姿态，以及一份逐项 pass/fail 的 `checks`。

完整自检（单元 + 无头 + 沙箱 + 实机 GUI）是一条命令，见 §8。

---

## 5. 接入你的客户端

### 5.1 让桥替你生成配置

QGIS 里：**Piterm QGIS AI → Client setup…** → 选客户端 → **Copy**（或 *Save to file*，
写盘前会备份并对 JSON 做合并）。配置片段由运行中的 host/端口/token 现场生成，永远与实况一致。

在终端或让 AI 自己取，同一条能力走线：

```powershell
python mcp\piterm_qgis_mcp\minimal.py call system.client_setup '{}'                    # 全部客户端
python mcp\piterm_qgis_mcp\minimal.py --json call system.client_setup '{"client":"cursor"}'
```

返回解析出的入口（`min_script` / `repo_root` / `source` / `full_available` / 人话 `note`）
以及可直接粘贴的配置：Claude Desktop（极简 **与** FastMCP 两版）、Claude Code、Cursor、
VS Code、Gemini CLI、Qwen Code、Kimi、Copilot CLI、LM Studio、opencode、Windsurf、
Codex（TOML）、Hermes（bat+YAML）、piterm 终端、以及"不用 MCP"的直连速查。

### 5.2 极简 MCP 服务器（推荐默认：零依赖）

```json
{ "mcpServers": { "piterm-qgis": {
    "command": "python",
    "args": ["G:/AIato/pi/piterm-qgis-ai/mcp/piterm_qgis_mcp/minimal.py", "--mcp-stdio"],
    "env": { "PITERM_QGIS_TOKEN": "<仅在启用 token 时>" } } } }
```

极简服务器同样把**整份目录**（195 个工具）通过 stdio 暴露出去，只是 schema 比 FastMCP 简单。

### 5.3 完整 FastMCP 服务器（更丰富的 schema / resources / prompts）

```json
{ "mcpServers": { "piterm-qgis": {
    "command": "uv",
    "args": ["--directory", "G:/AIato/pi/piterm-qgis-ai", "run", "--no-sync",
             "python", "-m", "piterm_qgis_mcp"] } } }
```

需要 `uv` 和 `pip install "mcp>=1.0"`。只有当仓库确实存在（`full_available: true`）时
`system.client_setup` 才会给出这一版——指向一个不存在的模块比不给更糟。

### 5.4 完全不用 MCP（技能脚本 / 终端 / 流水线）

```powershell
python mcp\piterm_qgis_mcp\minimal.py call layers.list '{}'
python mcp\piterm_qgis_mcp\minimal.py screenshot shot.png
python mcp\piterm_qgis_mcp\minimal.py batch '[{"type":"system.ping"},{"type":"layers.count"}]'

# HTTP JSON-RPC
curl -s http://127.0.0.1:9879/rpc -H "Content-Type: application/json" -d '{"type":"system.ping"}'

# 裸 TCP：一行一个 JSON（任何 shell / netcat 都行）
echo {"type":"layers.list"} | ncat 127.0.0.1 9877
```

### 5.5 环境变量（两种服务器与 CLI 通用）

| 变量 | 默认 | 含义 |
|---|---|---|
| `PITERM_QGIS_HOST` | `127.0.0.1` | 桥地址 |
| `PITERM_QGIS_PORT` | `9877` | TCP 端口 |
| `PITERM_QGIS_TOKEN` | – | `auth.enabled` 时的共享密钥 |
| `PITERM_QGIS_TIMEOUT` | `60` | 单次调用秒数 |
| `PITERM_QGIS_GZIP` | `0` | `1` → 接受 gzip 响应 |
| `PITERM_QGIS_TOOL_MODE` | `granular` | 工具数量受限的客户端可用 `compound` |
| `PITERM_QGIS_TRANSPORT` | `stdio` | 完整服务器可改 `streamable-http` |

---

## 6. 调用命令

### 6.1 CLI 表面

```
piterm-qgis [--host H] [--port P] [--token T] [--timeout S] [--client-name N]
            [--compress] [--json]
            {ping | hello | call | tools | screenshot | mcp-stdio | batch}
```

规则：**参数是 JSON，结果是 JSON**；失败是
`{"ok": false, "error": {"code": "E_…", "message": "…"}}` 且退出码非零，
可以直接进 CI 脚本。

### 6.2 配方（全部可跑）

```powershell
# —— 发现 ——
python mcp\piterm_qgis_mcp\minimal.py call system.hello '{}'                  # 握手
python mcp\piterm_qgis_mcp\minimal.py call system.capabilities '{}'           # 全量目录
python mcp\piterm_qgis_mcp\minimal.py call system.help '{"query":"checkpoint"}'
python mcp\piterm_qgis_mcp\minimal.py tools                                   # MCP 工具清单

# —— 载入与检查 ——
python mcp\piterm_qgis_mcp\minimal.py call layers.add_vector '{"path":"D:/data/roads.gpkg"}'
python mcp\piterm_qgis_mcp\minimal.py call layers.list '{}'
python mcp\piterm_qgis_mcp\minimal.py call layers.info '{"layer":"roads"}'
python mcp\piterm_qgis_mcp\minimal.py call layers.fields_list '{"layer":"roads"}'
python mcp\piterm_qgis_mcp\minimal.py call layers.count '{}'

# —— 查询 / 编辑 / 撤销（编辑是真写；不确定就先建检查点）——
python mcp\piterm_qgis_mcp\minimal.py call features.query '{"layer":"roads","filter":"\"lanes\" > 2","order_by":"length desc","limit":20}'
python mcp\piterm_qgis_mcp\minimal.py call features.insert '{"layer":"roads","features":[{"attributes":{"name":"new"},"geometry":{"wkt":"LINESTRING (0 0, 1 1)"}}]}'
python mcp\piterm_qgis_mcp\minimal.py call features.field_calculator '{"layer":"roads","field":"len2","expression":"$length*2"}'
python mcp\piterm_qgis_mcp\minimal.py call session.undo '{"layer":"roads","steps":1}'
python mcp\piterm_qgis_mcp\minimal.py call session.edit_status '{}'

# —— 海量读取不爆内存（分块游标）——
python mcp\piterm_qgis_mcp\minimal.py call features.cursor_open '{"layer":"roads","fields":["name"],"bbox":[0,0,1000,1000]}'
python mcp\piterm_qgis_mcp\minimal.py call features.cursor_fetch '{"cursor":"cur-1","n":500}'
python mcp\piterm_qgis_mcp\minimal.py call features.cursor_close '{"cursor":"cur-1"}'

# —— 样式与标注 ——
python mcp\piterm_qgis_mcp\minimal.py call style.renderer_set '{"layer":"roads","kind":"categorized","field":"class"}'
python mcp\piterm_qgis_mcp\minimal.py call style.renderer_set '{"layer":"roads","kind":"graduated","field":"length","classes":5,"ramp":"Spectral"}'
python mcp\piterm_qgis_mcp\minimal.py call style.labeling_set '{"layer":"roads","field":"name","enabled":true}'
python mcp\piterm_qgis_mcp\minimal.py call style.qml_save '{"layer":"roads","path":"D:/out/roads.qml"}'

# —— 出图（"给用户看一张图"就是这个）——
python mcp\piterm_qgis_mcp\minimal.py call layers.zoom_to '{"layer":"roads"}'
python mcp\piterm_qgis_mcp\minimal.py call canvas.render '{"width":1600,"height":900,"path":"D:/out/map.png"}'
python mcp\piterm_qgis_mcp\minimal.py screenshot D:/out/canvas.png

# —— Processing：同步（≤55 秒）vs 异步（无时限）——
python mcp\piterm_qgis_mcp\minimal.py call processing.execute '{"algorithm":"native:buffer","parameters":{"INPUT":"roads","DISTANCE":50,"OUTPUT":"memory:buf"}}'
python mcp\piterm_qgis_mcp\minimal.py call processing.start   '{"algorithm":"native:dissolve","parameters":{"INPUT":"plots","FIELD":["owner"],"OUTPUT":"memory:diss"},"load_results":true}'
python mcp\piterm_qgis_mcp\minimal.py call job.get     '{"id":"job-1"}'
python mcp\piterm_qgis_mcp\minimal.py call job.log     '{"id":"job-1"}'
python mcp\piterm_qgis_mcp\minimal.py call job.cancel  '{"id":"job-1"}'
python mcp\piterm_qgis_mcp\minimal.py call processing.algorithms_search '{"query":"zonal"}'

# —— 栅格 ——
python mcp\piterm_qgis_mcp\minimal.py call raster.stats '{"layer":"dem"}'
python mcp\piterm_qgis_mcp\minimal.py call raster.zonal_stats '{"raster":"dem","zones":"plots","stats":["mean","max"]}'
python mcp\piterm_qgis_mcp\minimal.py call raster.calculator '{"expression":"\"dem\" * 2","output":"memory:dem2"}'

# —— 布局与图集 ——
python mcp\piterm_qgis_mcp\minimal.py call layout.create '{"name":"A3"}'
python mcp\piterm_qgis_mcp\minimal.py call layout.add_map '{"layout":"A3","rect":[10,10,380,270]}'
python mcp\piterm_qgis_mcp\minimal.py call layout.add_legend '{"layout":"A3"}'
python mcp\piterm_qgis_mcp\minimal.py call layout.export '{"layout":"A3","path":"D:/out/a3.pdf"}'
python mcp\piterm_qgis_mcp\minimal.py call layout.atlas_configure '{"layout":"A3","layer":"plots"}'
python mcp\piterm_qgis_mcp\minimal.py call layout.atlas_export '{"layout":"A3","path":"D:/out/atlas.pdf"}'

# —— SQL（默认只读；写需要 allow_write 且数据源可写）——
python mcp\piterm_qgis_mcp\minimal.py call db.connections_list '{}'
python mcp\piterm_qgis_mcp\minimal.py call db.tables '{"connection":"pg_main"}'
python mcp\piterm_qgis_mcp\minimal.py call db.sql '{"connection":"pg_main","sql":"SELECT count(*) FROM roads"}'

# —— 多步工作流：$N 引用 + 先彩排 ——
python mcp\piterm_qgis_mcp\minimal.py call session.workflow_run '{"dry_run":true,"steps":[{"type":"layers.add_vector","params":{"path":"D:/data/a.gpkg"}},{"type":"processing.execute","params":{"algorithm":"native:buffer","parameters":{"INPUT":"$0.result.loaded_layers.0.id","DISTANCE":100,"OUTPUT":"memory:buf"}}}]}'
python mcp\piterm_qgis_mcp\minimal.py call session.workflow_run '{"steps":[...],"stop_on_error":true}'

# —— 检查点 / 日志账本（安全网）——
python mcp\piterm_qgis_mcp\minimal.py call session.checkpoint_create '{"name":"before-cleanup"}'
python mcp\piterm_qgis_mcp\minimal.py call session.checkpoint_list '{}'
python mcp\piterm_qgis_mcp\minimal.py call session.checkpoint_restore '{"checkpoint":"ckpt-20261009-233000-1"}'
python mcp\piterm_qgis_mcp\minimal.py call session.journal_list '{}'
python mcp\piterm_qgis_mcp\minimal.py call session.journal_export '{"path":"D:/out/session.jsonl"}'

# —— 一次往返发多条命令 ——
python mcp\piterm_qgis_mcp\minimal.py call system.batch '{"commands":[{"type":"layers.list"},{"type":"layers.count"}]}'

# —— 逃生口（可审计沙箱；exec_full 另有开关，见 §7）——
python mcp\piterm_qgis_mcp\minimal.py call python.exec '{"code":"print(project.fileName())"}'
```

每个响应都带 `meta.t_ms`（该命令耗时），不用开 profiler 就能从调用方侧做性能分析；
`system.metrics` 给总量 + 最耗时的前几名。

### 6.3 在你自己的 Python 里

```python
import sys
sys.path.insert(0, r"G:/AIato/pi/piterm-qgis-ai/mcp")
from piterm_qgis_mcp.minimal import BridgeClient          # 只用标准库

bridge = BridgeClient(host="127.0.0.1", port=9877)
print(bridge.ping())
print(bridge.call("layers.list", {}))
print(bridge.call("canvas.render", {"path": "D:/out/map.png", "width": 1600}))
```

技能包里的 `skill/piterm-qgis-ai/piterm_qgis.py` 是同一内核：
`python piterm_qgis.py ping`。

---

## 7. 安全与边界

默认姿态是"单人机器上好用，同时诚实说明剩下的风险"：绑回环、无 token、允许写、
允许 `execute`、路径白名单 `~` · `:TEMP:` · `:PROJECT:`、审计开启、`python.exec_full` 关闭。

| 控制项 | 怎么开 | 效果 |
|---|---|---|
| **token** | Dock 点 *Generate* + *Enforce token*；或先设 `auth.token` 再 `system.config_set {"key":"auth.enabled","value":true}` | 所有请求都必须带 token；同一连接连错 5 次 → `E_AUTH_LOCKED`；且此后才允许绑非回环地址 |
| **只读模式** | Dock 勾选，或 `mode.read_only=true` | 所有写/破坏/执行命令 → `E_FORBIDDEN` |
| **禁止执行代码** | `mode.allow_execute=false` | `python.exec*` → `E_FORBIDDEN` |
| **破坏前确认** | `mode.confirm_destructive=true` | 破坏类命令必须显式 `"__confirm": true` |
| **路径白/黑名单** | `paths.allow` / `paths.deny` | 越界返回 `E_PATH_DENIED` 并列出生效规则；黑名单优先 |
| **限速/连接数** | `limits.rate_per_sec`、`limits.max_clients`(16) | `E_BUSY`；可用 `system.clients_kick` |
| **完全 Python** | `dangerous.allow_full_python=true` **且** `mode.allow_execute=true` | `python.exec_full` = 任意代码 = 你的账号。等于开了一个 shell，谨慎 |

运行时读写策略：

```powershell
python mcp\piterm_qgis_mcp\minimal.py call system.config_get '{}'                     # 生效值 + security 块
python mcp\piterm_qgis_mcp\minimal.py call system.config_set '{"key":"mode.read_only","value":true}'
python mcp\piterm_qgis_mcp\minimal.py call system.diagnose '{}'                       # 一眼看 security
```

两个沙箱故意不同：`python.exec` 只允许 import `qgis`/`math`，封禁
`eval/exec/open/getattr/__import__` 这类逃逸；`python.exec_full` 全开，且需要额外开关。
**优先用专用命令而不是 `python.exec`**：专用命令有校验、可审计、可回滚。

取证材料都在配置目录里：`system.audit_tail`（JSONL，8 MB 轮转）、
`session.journal_list` / `session.journal_export`（可重放的变更账本）、
`session.checkpoint_*`（整工程快照，含内存图层）、`system.messages`（QGIS 日志环）。
出事时的顺序：踢客户端 → 读审计尾 → 导出账本 → 恢复检查点 → 轮换 token。

完整威胁模型与推荐配置见 `docs/SECURITY.md`。

---

## 8. 验证

```powershell
# 一条命令跑完整矩阵：pytest + 冒烟 + 沙箱覆盖 + 实机 GUI
powershell -ExecutionPolicy Bypass -File scripts\verify_all.ps1 -Sandbox

# 快速通道（跳过重活）
powershell -ExecutionPolicy Bypass -File scripts\verify_all.ps1 -SkipCoverage -SkipLive

# 单独跑
.venv\Scripts\python -m pytest tests -q                                                     # 单元，不需要 QGIS
"C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat" scripts\qgis_headless_smoke.py      # 63 项
"C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat" scripts\qgis_command_coverage.py --sandbox-db
python scripts\qgis_live_coverage.py 9877                                                   # 33 项（对着运行中的桥）
```

开关：`-Versions 3.44.15,4.2.3`、`-LivePorts 9877,9887`、`-SkipUnit`、`-SkipSmoke`、
`-SkipCoverage`、`-SkipLive`、`-Sandbox`。

全绿的样子（参考值，2026-10-09）：单测 **79 项**；每个构建冒烟 **63/63**；
沙箱覆盖 **177 passed / 0 hard fail**，目录全量被覆盖（无头 + 沙箱数据库写入 + 实机）；
每个运行实例实机 **33/33**；并集覆盖 **195/195 条命令**。整体约 40 秒。

证据：`verification/logs/*.json`（逐项、逐套件）以及完整控制台转录
`verification/logs/verify_all_<时间戳>.log`；叙述性报告在 `verification/report.md`。
`-Sandbox` **绝不碰你的真实配置**：它在抛弃式 settings root 上演练写连接的命令，
并给出哈希对比证据。

---

## 9. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `E_BRIDGE_DOWN` | QGIS 没开 / 桥没启动 / 端口不对——看 Dock 状态点和 `--port` |
| 找不到工具栏按钮 | 插件没启用：*插件 → 管理 → 搜 "Piterm"*（或重跑 `install.ps1`） |
| 启动报 `address in use` | 已有 QGIS/实例占着 9877——Dock 里换端口，或关掉重复实例 |
| 全是 `E_FORBIDDEN` | 开了只读或禁用了 execute——看 Dock 勾选项 / `system.config_get` |
| 保存报 `E_PATH_DENIED` | 把目录加进 `paths.allow`（`system.config_set`），或写到 `:TEMP:` / `:PROJECT:` |
| `E_AUTH` / `E_AUTH_LOCKED` | token 不匹配——用 *Client setup* 里复制出来的片段，别手改，再重连 |
| 客户端看不到工具 | 它必须能用 PATH 上的 `python` 启动，且 `minimal.py` 用**绝对路径**；先在那条命令的原样 shell 里试通 |
| Hermes 之类报 pydantic 导入错 | 用 **Hermes** 片段：它通过 `.bat` 清掉 `VIRTUAL_ENV`/`PYTHONPATH` 再启动 |
| `python.exec` 报 `E_EXEC ... not allowed` | 沙箱拦了某个 import/双下划线；改用专用命令，或确认风险后用 `python.exec_full` |
| 命令卡住然后 `E_TIMEOUT` | 长算法改 `processing.start`，再轮询 `job.get` |
| 读大图层很慢 | `features.cursor_open` + `cursor_fetch` 分页、`fields` 限定列、`bbox` 先过滤 |
| 图层名歧义（`E_AMBIGUOUS`） | `layers.find` 拿 id 后用 id 调用 |
| Windows 控制台中文乱码 | `chcp 65001`；各客户端脚本已强制 UTF-8 |
| QGIS 日志刷 "listening failed" | 起了多个桥实例——把多余的停掉 |
| *Client setup…* 顶部红字 "No MCP client script found" | 仓库搬家了：重跑 `install.ps1`（会内置客户端副本）或设置 `meta.repo_root` |

错误码速查：`E_BAD_REQUEST` `E_PARSE` `E_UNKNOWN_COMMAND` `E_AUTH` `E_AUTH_LOCKED`
`E_FORBIDDEN` `E_CONFIRM_REQUIRED` `E_NOT_FOUND` `E_AMBIGUOUS` `E_STATE` `E_INVALID`
`E_GEOMETRY` `E_CRS` `E_EXPRESSION` `E_IO` `E_PATH_DENIED` `E_TIMEOUT` `E_BUSY`
`E_PAYLOAD` `E_DISPATCH_GUARD` `E_CANCELLED` `E_INTERNAL` `E_EXEC`。
其中 `E_NOT_FOUND` / `E_AMBIGUOUS` 会在 `detail` 里给候选，先读候选再猜。

---

## 10. 更新 / 卸载

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -All      # 更新（旧版本留成 .bak-<时间戳>）
# 不重启 QGIS 重载插件：插件 → Piterm QGIS AI → Reload plugin
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
```

卸载会移除插件文件夹、把 ini 键置 `false` 并清空 `meta.repo_root`；
`<配置目录>/piterm_qgis_ai/` 下的检查点/审计/账本**故意保留**。
Agent 也可以从线内触发重载（显式保护：`plugins.reload piterm_qgis_ai` 会被拒绝，
请用 *Reload plugin*；或从 `python.exec` 里定时调用
`qgis.utils.unloadPlugin/loadPlugin/startPlugin`）。

---

## 11. 常见问答

**为什么完全不用线程？** PyQGIS 不是线程安全的。桥把 `QTcpServer` 跑在 Qt 事件循环上，
长任务交给 `QgsTask` 支撑的后台作业（`processing.start`）。所以它不会莫名其妙崩。

**怎么保证文档与实现一致？** 文档是生成的：`scripts/export_manifest.py` 产出
`docs/TOOLS.md`、`mcp/piterm_qgis_mcp/manifest.json`、`verification/manifest.json`；
另有单元测试钉住注册表形态。

**能进 CI 吗？** 无头部分可以：`qgis_headless_smoke.py` 与 `qgis_command_coverage.py`
自带 `QgsApplication`，不需要 GUI。纯 GUI 项（主题/书签缩放、切换活动图层）由实机套件覆盖。

**客户端该连 9877 还是 9879？** 说 MCP 的客户端走 stdio（没有端口）。`9877` 是裸套接字
（帧或 NDJSON）；`9879` 是 HTTP JSON-RPC（`/rpc`、`/health`、`/commands`、`/events`）。
三种入口的命令完全一样。

**195 条命令从哪来？** `plugin/piterm_qgis_ai/handlers/` 下 16 个模块里每个被
`@command(...)` 装饰的方法（声明名称、风险级别、标签）。全表见 `docs/TOOLS.md`，
分发管线见 `docs/ARCHITECTURE.md`。
