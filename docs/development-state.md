# 开发交接摘要

2026-11-10。完整路线见 [需求索引](requirements/README.md)，此处只保留续做所需状态。

- 技术：Python 3.12 / PySide6 / asyncio，Modbus TCP 与 S7 双端，无新增依赖。
- 分支检查点为基线 `codex/s7-workbench-baseline` 的 `16c12ab`、R1 的 `402993b`、R2 的 `925eca8`、R3 的 `df739b0`、R4 的 `ece6dc5`、R5 的 `2d03bc4`、R6 的 `ac82727` 和 R7 的 `a9e1cd0`。R8 的验收分支为 `codex/r8-diagnostics`，基于 `main` 的 `2ba9ced`。
- 已交付 R1 项目文件、R2 命名点表与 JSON/CSV、R3 公平队列和采样、R4 多设备独立会话、R5 服务端统一契约、R6 项目工作区和 R7 服务端场景模拟。详情见各需求文档。
- R4 结构：项目客户端为一个 IoBridge(SessionManager)，每设备一个 Session/调度器；本地模式仍用单 Session。MainWindow 缓存每设备状态、快速结果、采样及日志。
- 身份：DeviceEvent 携带 device_id、session_id、generation、runtime_id 和不可变连接配置。采样按设备独立，退休令牌和旧代次不能复活数据。日志新增设备/会话/代次，旧记录仍可读。
- 格式：项目 v3（兼容 v1/v2）含可选的 `workspace_layout` v1，点表 JSON v2（兼容 v1）。运行启停、采样和日志不随项目保存。所有设备共用本机最近 500 条日志，视图按设备 ID 过滤。
- R5 结构。服务端工作台从 `server_protocols.py` 获取适配器、能力、界面和存储。会话使用通用事件和类型化运行值接口，支持独立的 Modbus 和 S7 点位模型。新日志使用显式版本，旧日志及方案仍可读。
- R6 结构：`ProjectWindow` 持有同一组客户端和服务端视图，项目树切换不停止 I/O；确认关闭后等待全部会话清理。全量 185 项回归通过；150% 缩放和低 GC 阈值下 20 项项目界面回归通过。
- R7 由 `ServerSession` 管理每个监听的场景时钟与定时任务，经 R5 运行值接口写入。适配器报告成功写入的地址范围，S7 另附位掩码。点位各保存一条规则，运行值、接管及故障只保留在监听会话。含规则的 Modbus 方案使用 v3、S7 使用 v2。全量 204 项测试通过。
- R8 请求诊断复用已捕获日志，每 500 ms 刷新筛选结果，选中报文时按需解码。新服务端日志增加可选的耗时、错误和会话／请求编号。故障规则由 `ServerSession` 管理，经适配器回调匹配，停止监听时由适配器取消延迟任务。全量 216 项测试通过。 [R8 验收细节](requirements/R8-diagnostics.md)
- R9 的验收分支为 `codex/r9-write-verification`，基于 R8 的 `0a9feee`。写入与自动回读作为同一设备队列的相邻操作。客户端日志保存结构化写入和验证证据，独立核对通过原写入的会话与请求 ID 关联。全量 233 项测试通过。 [R9 验收细节](requirements/R9-write-verification.md)
- R10 的验收分支为 `codex/r10-reports`，基于 R9 的 `09c30a7`。项目工作区冻结配置与有界日志后在后台生成预览和报告包，客户端按设备 ID 匹配，服务端按本次工作区采集。全量 240 项测试通过。 [R10 验收细节](requirements/R10-reports.md)
- R1–R10 开发路线已完成。
- 功能测试修复 `2f075ee` 已快进合并到本地 `main`，包含 Modbus TCP / S7 响应校验和项目报告修复，全量 245 项测试通过。下一阶段见[协议扩展开发路线](requirements/protocol-expansion.md)。
- 常用命令：`.venv/Scripts/python.exe -m pytest -q`、`git diff --check`。离屏截图需显式加载系统 msyh.ttc；产品字体不变。
- Git：每项独立分支与提交，不自动合并或推送，结束时保持干净工作区。
