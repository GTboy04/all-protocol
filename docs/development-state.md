# 开发交接摘要

2026-11-07。完整路线见 [需求索引](requirements/README.md)，此处只保留续做所需状态。

- 技术：Python 3.12 / PySide6 / asyncio，Modbus TCP 与 S7 双端，无新增依赖。
- 分支检查点：基线 `codex/s7-workbench-baseline`=`16c12ab`；R1=`402993b`；R2=`925eca8`；R3=`df739b0`。当前 `codex/r4-multi-session` 已完成 R4，按该分支 HEAD 继续，历史需求分支均保留。
- 已交付：R1 项目文件；R2 命名点表与 JSON/CSV；R3 公平队列、多组轮询、合并读取及 SampleStore；R4 多设备独立会话。详情见各需求文档。
- R4 结构：项目客户端为一个 IoBridge(SessionManager)，每设备一个 Session/调度器；本地模式仍用单 Session。MainWindow 缓存每设备状态、快速结果、采样及日志，切换不停止后台任务；R6 再拆分统一工作区。
- 身份：DeviceEvent 携带 device_id、session_id、generation、runtime_id 和不可变连接配置。采样按设备独立，退休令牌和旧代次不能复活数据。日志新增设备/会话/代次，旧记录仍可读。
- 格式：项目 v3（兼容 v1/v2），点表 JSON v2（兼容 v1），运行启停/采样不保存。所有设备共用本机最近 500 条日志，视图按设备过滤。
- 验证：180 项常规回归及低 GC 阈值压力检查通过；双设备界面截图检查通过。未重新打包 Windows 产物。
- 下一项：从已提交的 R4 创建 `codex/r5-server-contract`，读取 [R5 文档](requirements/R5-server-contract.md)。`application/server_session.py` 仍依赖具体 Modbus 服务器和日志类型；R5 统一服务端接口/注册/事件，保留旧方案和日志兼容，不重写客户端 Manager。
- 常用命令：`.venv/Scripts/python.exe -m pytest -q`、`git diff --check`。离屏截图需显式加载系统 msyh.ttc；产品字体不变。
- Git：每项独立分支与提交，不自动合并或推送，结束时保持干净工作区。
