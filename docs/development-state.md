# 开发交接摘要

2026-11-09。完整路线见 [需求索引](requirements/README.md)，此处只保留续做所需状态。

- 技术：Python 3.12 / PySide6 / asyncio，Modbus TCP 与 S7 双端，无新增依赖。
- 分支检查点为基线 `codex/s7-workbench-baseline` 的 `16c12ab`、R1 的 `402993b`、R2 的 `925eca8`、R3 的 `df739b0`、R4 的 `ece6dc5` 和 R5 的 `2d03bc4`。R6 已完成，续做从 `codex/r6-workspace` 的 HEAD 开始。
- 已交付 R1 项目文件、R2 命名点表与 JSON/CSV、R3 公平队列和采样、R4 多设备独立会话、R5 服务端统一契约和 R6 项目工作区。详情见各需求文档。
- R4 结构：项目客户端为一个 IoBridge(SessionManager)，每设备一个 Session/调度器；本地模式仍用单 Session。MainWindow 缓存每设备状态、快速结果、采样及日志。
- 身份：DeviceEvent 携带 device_id、session_id、generation、runtime_id 和不可变连接配置。采样按设备独立，退休令牌和旧代次不能复活数据。日志新增设备/会话/代次，旧记录仍可读。
- 格式：项目 v3（兼容 v1/v2）含可选的 `workspace_layout` v1，点表 JSON v2（兼容 v1）。运行启停、采样和日志不随项目保存。所有设备共用本机最近 500 条日志，视图按设备 ID 过滤。
- R5 结构。服务端工作台从 `server_protocols.py` 获取适配器、能力、界面和存储。会话使用通用事件和类型化运行值接口，支持独立的 Modbus 和 S7 点位模型。新日志使用显式版本，旧日志及方案仍可读。
- R6 结构：`ProjectWindow` 持有同一组客户端和服务端视图，项目树切换不停止 I/O；确认关闭后等待全部会话清理。全量 185 项回归通过；150% 缩放和低 GC 阈值下 20 项项目界面回归通过。
- 下一项为 [R7 服务端场景模拟](requirements/R7-simulation.md)。从已提交的 R6 切出独立分支，复用 R5 运行值接口及 R6 服务端方案/当前值视图。
- 常用命令：`.venv/Scripts/python.exe -m pytest -q`、`git diff --check`。离屏截图需显式加载系统 msyh.ttc；产品字体不变。
- Git：每项独立分支与提交，不自动合并或推送，结束时保持干净工作区。
