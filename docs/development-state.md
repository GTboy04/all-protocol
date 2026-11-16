# 开发交接摘要

2026-11-15。完整路线见 [需求索引](requirements/README.md)，此处只保留续做所需状态。

- 技术：Python 3.12 / PySide6 / asyncio，Modbus TCP 与 S7 双端；R11 增加 Modbus RTU 预览版及 pyserial 3.5。
- 分支检查点为基线 `codex/s7-workbench-baseline` 的 `16c12ab`、R1 的 `402993b`、R2 的 `925eca8`、R3 的 `df739b0`、R4 的 `ece6dc5`、R5 的 `2d03bc4`、R6 的 `ac82727` 和 R7 的 `a9e1cd0`。R8 的验收分支为 `codex/r8-diagnostics`，基于 `main` 的 `2ba9ced`。
- 已交付 R1 项目文件、R2 命名点表与 JSON/CSV、R3 公平队列和采样、R4 多设备独立会话、R5 服务端统一契约、R6 项目工作区和 R7 服务端场景模拟。详情见各需求文档。
- R4 结构：项目客户端为一个 IoBridge(SessionManager)，每设备一个 Session/调度器；本地模式仍用单 Session。MainWindow 缓存每设备状态、快速结果、采样及日志。
- 身份：DeviceEvent 携带 device_id、session_id、generation、runtime_id 和不可变连接配置。采样按设备独立，退休令牌和旧代次不能复活数据。日志新增设备/会话/代次，旧记录仍可读。
- 格式：项目 `.allprotocol` 使用配置 v4 / SQLite user_version 2（兼容读取旧 SQLite 和 JSON 项目 v1/v2/v3），含可选的 `workspace_layout` v1；本地配置和日志使用 `allprotocol.sqlite3`，旧 JSON/JSONL 按来源自动迁移且保留原件。点表 JSON v2（兼容 v1）。运行启停、采样和日志不随项目保存。所有设备共用本机最近 500 条日志，视图按设备 ID 过滤。
- R5 结构。服务端工作台从 `server_protocols.py` 获取适配器、能力、界面和存储。会话使用通用事件和类型化运行值接口，支持独立的 Modbus 和 S7 点位模型。新日志使用显式版本，旧日志及方案仍可读。
- R6 结构：`ProjectWindow` 持有同一组客户端和服务端视图，项目树切换不停止 I/O；确认关闭后等待全部会话清理。全量 185 项回归通过；150% 缩放和低 GC 阈值下 20 项项目界面回归通过。
- R7 由 `ServerSession` 管理每个监听的场景时钟与定时任务，经 R5 运行值接口写入。适配器报告成功写入的地址范围，S7 另附位掩码。点位各保存一条规则，运行值、接管及故障只保留在监听会话。含规则的 Modbus 方案使用 v3、S7 使用 v2。全量 204 项测试通过。
- R8 请求诊断复用已捕获日志，每 500 ms 刷新筛选结果，选中报文时按需解码。新服务端日志增加可选的耗时、错误和会话／请求编号。故障规则由 `ServerSession` 管理，经适配器回调匹配，停止监听时由适配器取消延迟任务。全量 216 项测试通过。 [R8 验收细节](requirements/R8-diagnostics.md)
- R9 的验收分支为 `codex/r9-write-verification`，基于 R8 的 `0a9feee`。写入与自动回读作为同一设备队列的相邻操作。客户端日志保存结构化写入和验证证据，独立核对通过原写入的会话与请求 ID 关联。全量 233 项测试通过。 [R9 验收细节](requirements/R9-write-verification.md)
- R10 的验收分支为 `codex/r10-reports`，基于 R9 的 `09c30a7`。项目工作区冻结配置与有界日志后在后台生成预览和报告包，客户端按设备 ID 匹配，服务端按本次工作区采集。全量 240 项测试通过。 [R10 验收细节](requirements/R10-reports.md)
- R1–R10 开发路线已完成。
- 功能测试修复 `2f075ee` 已快进合并到本地 `main`，包含 Modbus TCP / S7 响应校验和项目报告修复，全量 245 项测试通过。下一阶段见[协议扩展开发路线](requirements/protocol-expansion.md)。
- 当前 `main` 已包含全部功能验收分支，并加入 SQLite 本地与项目存储、旧 JSON/JSONL 迁移，以及共用日志阅读区（专注日志、独立窗口、布局切换和自动换行）。2026-11-13 全量 285 项测试通过；README 使用当前程序运行截图，并补充诊断、故障注入、写入回读及报告说明。
- 常用命令：`.venv/Scripts/python.exe -m pytest -q`、`git diff --check`。离屏截图需显式加载系统 msyh.ttc；产品字体不变。
- Git：每项独立分支与提交，不自动合并或推送，结束时保持干净工作区。

- R11 软件预览版位于 codex/r11-modbus-rtu，新增串口双端、项目 v4 / SQLite user_version 2、点表、回读与 CRC 诊断。全量 348 项通过、3 项跳过，Windows 构建和基础启动通过；实机串口与拔插待验收。见 [交付记录](r11-modbus-rtu.md)。

- R12 OPC UA 预览版位于 `codex/r12-opc-ua`，承接未提交的 R11 和点位排序改动。使用 asyncua 1.1.8；独立同步 python-opcua 0.98.13 仅为开发测试依赖。客户端支持发现、浏览、证书安全读写、点表轮询和订阅；服务端为显式启用的回环匿名模拟，支持场景推送。OPC UA 项目使用 v5 / SQLite user_version 3，旧项目继续兼容。证书路径和私钥留在本机。全量 376 通过、3 跳过，Windows 构建及基础启动通过；独立厂商设备和现场打包版互通待验收。见 [交付记录](r12-opc-ua.md)。

- 2026-11-15 新增 R13–R19 独立开发计划，均未实施：R13 现场验收与恢复、R14 趋势录制、R15 联调用例、R16 点表与模板、R17 异常取证、R18 性能可观测性、R19 协议选型。建议先推进 R13 与 R18 基线，再交付 R14、R15；R16 可独立插入，R17 依赖 R14，R19 按设备条件立项。目标、依赖和各计划入口见[下一阶段计划](requirements/README.md#下一阶段计划2026-11-15)。本次仅补充规划，不改变现有交付状态与验收结论。

- 2026-11-15 后续开发 R13 软件与用户追加的 R20，位于 `codex/r13-runtime-reliability`，保留承接的 R11/R12 未提交工作。新增设备级可控重连、读取采集恢复、订阅保活与值时间区分，以及点位多行功能描述、设备内跨点表搜索定位和报告描述。自定义恢复配置使用项目 v6 / SQLite user_version 3，点表沿用已有 `description`。现场硬件、厂商设备与 24 小时稳定性仍待验收，R14–R19 保持规划状态。实现及验证见 [R13 阶段交付记录](r13-runtime-reliability.md)，点位功能见 [R20](requirements/R20-point-definitions.md)。

- R13/R20 软件全量回归 **406 通过、3 跳过**；`git diff --check`、Windows 构建及离屏基础启动检查通过。构建输出 `dist/r13/AllProtocol/AllProtocol.exe`，不覆盖原有 `dist/AllProtocol`。测试固定 QApplication 生命周期并在 GUI 线程清理旧对象，防止全量用例中 Qt 窗口回收导致的原生退出。未提交、合并或推送。
