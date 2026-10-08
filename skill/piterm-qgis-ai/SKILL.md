---
name: piterm-qgis-ai
description: 通过 Piterm QGIS AI 桥直接操控本机 QGIS（TCP 帧/NDJSON/HTTP 三种入口，160+ 命令）。当任务涉及 QGIS 工程、图层、要素编辑、Processing 算法、渲染截图、布局出图、检查点回滚或批量制图工作流时使用。也提供 piterm-qgis CLI 与 MCP stdio 服务器两种接入方式。
---

# Piterm QGIS AI · 技能

把正在运行的 QGIS 变成可编程工具。桥由 QGIS 插件托管（`piterm_qgis_ai`），
本技能用零依赖脚本 `piterm_qgis.py` 直连，无需 MCP 客户端、无需 pip 安装。

## 0. 前置检查

```bash
python piterm_qgis.py ping            # {"pong": true, ...} 说明 QGIS 在线
python piterm_qgis.py hello           # 协议/命令数/安全姿态/端口
```

- 默认端点：`127.0.0.1:9877`（TCP 帧或 NDJSON 行）；HTTP：`127.0.0.1:9879`。
- 端口/主机可在 QGIS Dock「Piterm QGIS AI」里改；环境变量覆盖：
  `PITERM_QGIS_HOST` / `PITERM_QGIS_PORT` / `PITERM_QGIS_TOKEN`。
- 若连接失败：① QGIS 是否开着？② Dock 里服务器是否 Start？(图标变绿)
  ③ 防火墙是否拦截 127.0.0.1？（正常不会）。

## 1. 调用模型

```bash
python piterm_qgis.py call layers.list '{}'
python piterm_qgis.py call features.query '{"layer":"roads","limit":5,"geometry":"wkt"}'
python piterm_qgis.py --json call system.capabilities | head -c 400
```

- 参数是 JSON；返回是 JSON。`{"ok":false,"error":{"code":"E_...","message":"..."}}`。
- 错误码速查：`E_NOT_FOUND`(图层/字段不存在，detail 里带候选) ·
  `E_AMBIGUOUS`(名字撞了多个，用 layer id) · `E_FORBIDDEN`(只读模式/未开放执行) ·
  `E_PATH_DENIED`(路径不在白名单) · `E_CONFIRM_REQUIRED`(加 `"__confirm":true`) ·
  `E_EXPRESSION`(QGIS 表达式错) · `E_TIMEOUT`。
- 先看清单再动手：`system.help`（搜索）、`system.capabilities`（全量+参数）、
  `layers.info`（字段/CRS/编辑态）。

## 2. 高频配方

### 2.1 载入与检查数据
```bash
python piterm_qgis.py call layers.add_vector '{"path":"D:/data/roads.gpkg"}'
python piterm_qgis.py call layers.info '{"layer":"roads"}'
python piterm_qgis.py call features.field_stats '{"layer":"roads","field":"length"}'
```

### 2.2 查询与编辑（可撤销）
```bash
python piterm_qgis.py call features.query '{"layer":"roads","filter":"\"lanes\" > 2","order_by":"length desc","limit":20}'
python piterm_qgis.py call features.insert '{"layer":"roads","features":[{"attributes":{"name":"new"},"geometry":{"wkt":"LINESTRING (0 0, 1 1)"}}]}'
python piterm_qgis.py call session.undo '{"layer":"roads","steps":1}'
```

### 2.3 跑算法 / 长任务
```bash
# 同步（55s 上限，插件侧先超时并说明原因）
python piterm_qgis.py call processing.execute '{"algorithm":"native:buffer","parameters":{"INPUT":"roads","DISTANCE":50,"OUTPUT":"memory:buf"}}'
# 异步（无时限，轮询 job.get）
python piterm_qgis.py call processing.start '{"algorithm":"native:dissolve","parameters":{"INPUT":"plots","FIELD":["owner"],"OUTPUT":"memory:diss"},"load_results":true}'
python piterm_qgis.py call job.get '{"id":"job-1"}'
```
- 结果落盘：`OUTPUT` 传真实路径（受路径白名单约束）；临时结果传 `memory:名字`。
- 大范围栅格分析建议 `processing.start`；`job.cancel` 可中止。

### 2.4 渲染与截图（给用户看）
```bash
python piterm_qgis.py call layers.zoom_to '{"layer":"roads"}'
python piterm_qgis.py call canvas.render '{"width":1600,"path":"D:/out/map.png"}'
python piterm_qgis.py screenshot D:/out/canvas.png
```

### 2.5 多步工作流（带 `$N.字段` 引用 + dry-run）
```bash
python piterm_qgis.py call session.workflow_run '{
  "steps":[
    {"type":"layers.add_vector","params":{"path":"D:/data/a.gpkg"}},
    {"type":"processing.execute","params":{"algorithm":"native:buffer","parameters":{"INPUT":"$0.result.layer.id","DISTANCE":100,"OUTPUT":"memory:buf"}}},
    {"type":"layers.count","params":{"layer":"$1.result.loaded_layers.0.id"}}
  ],"stop_on_error":true}'
# 先彩排：
python piterm_qgis.py call session.workflow_run '{"steps":[...],"dry_run":true}'
```

### 2.6 危险前先存档
```bash
python piterm_qgis.py call session.checkpoint_create '{"name":"before-cleanup"}'
# ... 大改 ...
python piterm_qgis.py call session.checkpoint_restore '{"checkpoint":"ckpt-20261008-120000-1"}'
```

### 2.7 与 AI 客户端 / MCP 对接
```bash
python piterm_qgis.py mcp-stdio          # 零依赖 MCP stdio 服务器
python -m piterm_qgis_mcp serve          # FastMCP 版（需 pip install mcp）
```
QGIS 里「Piterm QGIS AI → Client setup…」可一键复制 15 种客户端配置
（Claude/Cursor/VS Code/Gemini/Qwen/Kimi/Copilot/LM Studio/opencode/Hermes/Codex…）。

## 3. 安全与边界（务必遵守）

- **只读模式**：Dock 勾选后所有写命令返回 `E_FORBIDDEN`；不确定时先 `system.config_get` 看策略。
- **token**：启用后所有请求必须带 `PITERM_QGIS_TOKEN`（或 Dock 里复制出的配置）。
- **路径白名单**：文件读写命令只允许 `paths.allow` 内的路径；被拒是 `E_PATH_DENIED`，不要绕过。
- **`__confirm`**：开启 confirm 模式后，破坏性命令需要显式 `"__confirm":true`。
- **日志**：所有命令进审计（`system.audit_tail`）；变更进日志账本
  （`session.journal_list`，可导出可重放的 `session.journal_export`）。
- 不要用 `python.exec_full` 做本可用专用命令完成的事；优先专用命令（可审计、可撤销）。

## 4. 故障排查

| 现象 | 处理 |
|---|---|
| `E_BRIDGE_DOWN` | QGIS 未启动 / Dock 未 Start；检查端口 |
| 命令卡住后 `E_TIMEOUT` | 用 `processing.start` 转异步；看 `job.get` |
| 读要素很慢 | `features.cursor_open` 分页；`fields` 限定列；`bbox` 先过滤 |
| 图层名歧义 | `layers.find` 拿 id 后用 id 调用 |
| 中文乱码 | 终端 `chcp 65001`；脚本已强制 UTF-8 |
| 想看 QGIS 报错 | `system.messages`（桥内日志环） |

## 5. 参考

- `docs/TOOLS.md`：全部命令速查（自动生成）。
- `docs/PROTOCOL.md`：帧/NDJSON/HTTP 协议与事件。
- `docs/SECURITY.md`：策略矩阵与建议配置。
