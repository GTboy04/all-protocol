# 开发交接摘要

日期：2026-11-07。完整路线见 [需求索引](requirements/README.md)。本文件只保留继续开发所需状态。

- 技术：Python 3.12 / PySide6 / asyncio；Modbus TCP、S7 双端；无新增依赖。
- 分支：基线 `codex/s7-workbench-baseline` → `16c12ab`；R1 `codex/project-workspace-r1` → `402993b`；R2 `codex/r2-point-tables` → `925eca8`；当前 `codex/r3-scheduling` 已完成 R3，按分支 HEAD 继续。
- R1：独立项目与本地模式，项目文件原子保存、外部修改检测、旧配置复制导入；打开不自动联网。切换项目前需关闭工作窗口。日志仍在本机。
- R2：命名点表、JSON/CSV 校验预览、原始值写入；项目模式客户端「点表」页。
- R3：已完成统一队列、多组轮询、连续地址合并及独立 SampleStore。32 手动任务/64 轮询组，最多 3 个手动请求后让到期组执行；停止不打断在途请求，断开清空队列，不重放写入。161 项常规回归及低 GC 阈值压力检查通过，详见 [开发文档](requirements/R3-scheduling.md)。
- 格式：项目 v3（兼容 v1/v2），点表 JSON v2（兼容 v1）；运行启停/采样不保存，旧日志缺少 operation_id 仍可读。
- 下一项：从已提交的 R3 创建 `codex/r4-multi-session`，读取 [R4 文档](requirements/R4-multi-session.md)。Session 已自带调度器，SampleStore 以 generation 丢弃旧响应；R4 需以 device_id 管理多个 Session、转发事件时携带设备与会话身份，不能让不同 Session 的相同 generation 相互覆盖。当前 MainWindow 仍绑定一个 IoBridge/Session。
- 常用验证：`.venv/Scripts/python.exe -m pytest -q`、`git diff --check`。Qt 离屏环境没有默认字体时，截图检查需显式加载系统 msyh.ttc；产品沿用原有字体设置。
- Git：每个需求独立分支与提交，不自动合并或推送；任务结束保留干净工作区。原有改动已分别提交，不再存在混合暂存问题。
