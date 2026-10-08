# 对标研究：QGIS AI 工具生态（源码级）

> 结论先行：**QGIS 内核没有内建 AI 工具层**；生态里最完整的单桥是
> nkarasiak/qgis-mcp（125 工具），最早的经典是 jjsantos01/qgis_mcp（15 工具），
> 工具质量最高的是 opengeos/GeoAgent（29 个 QGIS 工具 + 确认钩子 + AST 沙箱）。
> Piterm QGIS AI 在它们的基础上做了「全都要 + 全部补强」：架构借鉴、坑位规避、
> 能力面直接超过 125（当前 160+ 命令且由注册表自证）。

## 0. 生态扫描（本机可复算）

| 口径 | 数字 | 来源 |
|---|---|---|
| MCP 桥最多工具数 | **125**（nkarasiak/qgis-mcp，`@mcp.tool` 计数） | 本机下载源码复算 |
| 最早/最知名 | **15**（jjsantos01/qgis_mcp，README Tools 段） | 本机下载源码 |
| 插件内嵌 Agent | **29** QGIS 工具（GeoAgent，`@geo_tool`） | 本机下载源码 |
| 官方插件库 MCP 类插件 | ~10 个真桥（plugins.qgis.org 搜 “MCP” 12 条） | 插件库页面 |
| QGIS 原生 Processing 算法 | 271 个（master `qgsalgorithm*.cpp`） | 用户清单文档（2026-10-08 实测） |
| 兜底面 | `execute_processing` + `execute_code` = 不可枚举 | 同上 |

下载与核对命令（本机实际使用）：

```powershell
# GitHub 直连不通 → 走 gh-proxy 镜像（下载通道验证见 verification/）
curl.exe -L "https://gh-proxy.com/https://github.com/nkarasiak/qgis-mcp/archive/refs/heads/main.zip" -o nkarasiak.zip
curl.exe -L "https://gh-proxy.com/https://github.com/jjsantos01/qgis_mcp/archive/refs/heads/main.zip" -o jjsantos01.zip
curl.exe -L "https://gh-proxy.com/https://github.com/opengeos/GeoAgent/archive/refs/heads/main.zip" -o geoagent.zip
# 单文件源码亦可走 jsdelivr：https://cdn.jsdelivr.net/gh/<user>/<repo>@main/<path>
```

## 1. nkarasiak/qgis-mcp（125 工具）：机制与坑位

**架构**：AI ←→ FastMCP(MCP stdio) ←→ TCP 长度前缀帧 ←→ QGIS 插件（QTimer 事件循环）←→ PyQGIS。

关键机制（值得抄的部分）：

1. **零线程**：非阻塞 socket + QTimer(25ms) 跑在 QGIS 事件循环里，没有任何线程碰
   PyQGIS（非线程安全）。`_in_dispatch` 重入闸防止长 handler 里 `processEvents()`
   时被第二个命令插入执行（编辑会话/工程状态会被交错）。
2. **分帧**：4 字节大端长度前缀 + JSON；入站 10 MB、每连接出站积压 64 MB 上限；
   读缓冲用 `start` 偏移 + `finally` 回收，避免大请求 O(n²)；出站 `OutboundBuffer`
   处理部分写，绝不在非阻塞 socket 上用 `sendall`（会出现半帧永久错位）。
3. **JSON 安全**：`_json_safe` 递归把 NaN/Infinity 换成 `None`（空图层 extent、
   无数据栅格统计天然产出 NaN，严格 JSON 解析器会拒）。
4. **版本握手**：client 每命令上报版本 + 安装方式（uvx/source），插件在 `diagnose`
   与日志里给出升级命令；两侧不同步时提示缺失工具。
5. **审计与回放**：`create_checkpoint/restore_checkpoint`（内存图层在内的整工程
   快照）、`export_session`（把变更命令导出成可重放 Python 脚本，layer id 重映射）。
6. **批量限制**：`BATCH_BLOCKED_COMMANDS`（execute_code、remove_layer 等）禁止套进
   `batch`，插件端与 MCP 端两份清单并用测试钉住一致。
7. **确认流**：MCP 层用 elicitation（客户端弹确认），默认跳过（客户端自己会问），
   `QGIS_MCP_AUTO_CONFIRM=0` 可强制。
8. **compound 模式**：客户端工具数受限时把 125 个工具折叠成 27 个分组工具
   （`action` + `params`），schema 开销骤降。

**坑位（我们逐一规避/改进）**：

- `QgsProject.read()` 可能弹模态 “Handle Unavailable Layers” 卡死 socket——他们用
  50 ms QTimer 自动 `reject()`；我们复刻进 `compat.read_project_guarded()`。
- `QgsExpression` 校验必须 `prepare()` 后才算数，否则“无字段的过滤器”不报错却匹配
  0 条；节点持有期在 QGIS 4 有原生崩溃风险（必须持有 QgsExpression 对象）。
- PyQGIS 把 Qt 日期类型拆包返回，日期分支要对裸值做 `toPyDateTime`。
- 渲染要克隆 `canvas.mapSettings()` 而不是重放 `project.mapLayers()`（隐藏图层会盖住
  可见图层——真实 issue #21），并把 `render.errors()` 当 warnings 回传。
- `isinstance` 不能用 PEP604 union（QGIS 3.42 仍绑 Python 3.9，运行期 TypeError）。
- Windows 上 `SO_REUSEADDR` 会让两个 QGIS 同端口“双双启动成功”并静默吞连接，
  必须 `SO_EXCLUSIVEADDRUSE`。

## 2. jjsantos01/qgis_mcp（15 工具）：为什么它是反面教材

最小实现（QObject + 100ms QTimer + `json.loads(buffer)`）足够教学，但：

- **无分帧**：一次 `recv(8192)` 含两条消息或尾随垃圾 → 永久 JSONDecodeError，
  buffer 只增不减，后续全卡死。
- **单客户端**：`listen(1)` + 只在无 client 时 accept；半死连接挡住新会话。
- **execute_code 无沙箱无确认**：完整 builtins 直接 exec。
- **stdout 交换不在 finally**：BaseException（SystemExit/KeyboardInterrupt）会永久
  劫持 QGIS 的 stdout。
- 处理期间同步阻塞 GUI（processing、渲染 `waitForFinished()`）。

→ 我们的改进：分帧 + text 模式自动识别；`MAX_CLIENTS=16`；`python.exec` 默认 AST
沙箱（仅 qgis/math、禁 open/eval/dunder、受限 builtins）+ 超时 trace 钩子；
stdout/stderr 全部 `redirect_stdout` 上下文管理器（异常路径也恢复）；
长任务一律走 `job.*`（QgsTask），同步命令显式 55s 插件侧超时。

## 3. opengeos/GeoAgent（29 工具）：安全与线程封送模板

- **Qt 主线程封送** `run_on_qt_gui_thread`：非 GUI 线程用 `QMetaObject.invokeMethod
  + BlockingQueuedConnection` 把调用搬进 GUI 线程；比较线程必须用相等（`==`）而非
  `is`（PyQt 可能用不同 Python 对象包同一 C++ QThread，误判会死锁）；invoker 在
  finally `moveToThread` 回原线程，否则 Python 在 worker 线程销毁 GUI 亲和对象会崩。
  ——我们的服务器天然全在主线程（QTcpServer），不需要封送；该模式作为文档留档，
  给未来「把 MCP 服务器嵌进 QGIS」的扩展用。
- **AST 沙箱清单**（允许 qgis/math 及子模块、禁相对导入、禁
  `__import__/compile/eval/exec/input/open` 直呼、禁 dunder 属性、禁
  `__builtins__/__dict__/__globals__/__subclasses__` 名字、22 个白名单 builtins）
  ——我们原样吸收并加严：禁 `getattr/setattr/delattr/type/object/super` 等逃逸口，
  加 wall-clock 超时 trace 钩子，错误以结构化 `E_EXEC` 返回并携带 stdout。
- **确认钩子**（`ConfirmRequest{tool_name,args,description,category,metadata}` +
  `requires_confirmation/destructive/long_running` 元数据 + 拒绝即 cancel）
  ——我们抽象成 wire 层 `mode.confirm_destructive` + `__confirm` 参数（任何客户端
  都能用，不绑定 Strands），MCP 端保留注解（readOnly/destructive）供客户端自己的
  确认 UI 使用。

## 4. 其他桥（README 级扫描）

官方插件库 ~10 个真桥（QGIS MCP / AI GIS AGENT / QGIS Agent MCP / QGIS Agent /
Salah MCP / WAI QGIS MCP / SIGMAI / ClaudIA / AtQuery …），功能高度重叠，均
“QGIS 内 socket + 外部协议端”。共识架构 = 我们采用的架构；差异点集中在工具数、
安全门禁与客户端集成面——这三项恰好是我们的主攻方向。

## 5. 设计取舍对照表

| 维度 | 生态现状 | Piterm QGIS AI 的选择 |
|---|---|---|
| 工具数 | 15 / 125 | **160+**，注册表自证数量，AST 测试防漂移 |
| 传输 | 帧（多数）/ 仅 socket | **帧 + NDJSON + HTTP JSON-RPC** 三入口，同一 dispatcher |
| MCP 依赖 | 必需 FastMCP | **双模式**：FastMCP 全功能 / stdlib 零依赖 minimal |
| 线程模型 | QTimer 零线程（A-1）| 同样零线程，但用 **QTcpServer + 信号**，代码量更小、无自旋 |
| 确认 | elicitation 或没有 | 策略层 `__confirm` + MCP 注解双保险 |
| 沙箱 | GeoAgent 有，MCP 桥没有 | 默认沙箱 + 显式开关的 full 模式（双重开关） |
| 路径控制 | 基本没有 | 允许/拒绝清单 + `:TEMP:`/`:PROJECT:` 占位符 |
| 回滚 | checkpoint（A-1） | checkpoint + **journal 回放脚本** + undo/redo 命令 |
| 工作流 | batch（A-1） | batch + **workflow.run（`$N.field` 引用 / dry-run）** |
| 诊断 | diagnose（A-1） | diagnose + metrics + 消息环 + 审计尾 + 客户端踢除 |
| 文档同步 | 手写清单 | **自动生成 TOOLS.md / manifest.json**（QGIS 内导出） |

## 6. 复算与再验证

本文所有数字可由以下命令复现（见 `verification/logs/`）：

```powershell
# 工具数（AST，不依赖 QGIS）
python -m pytest tests/test_manifest.py -q
# 注册表全量（需要 QGIS）
"C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat" scripts/export_manifest.py
```
