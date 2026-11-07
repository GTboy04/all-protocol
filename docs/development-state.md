# 开发交接摘要

日期：2026-11-07。完整路线见 [需求索引](requirements/README.md)。本文件只保留继续开发所需状态。

- 技术：Python 3.12 / PySide6 / asyncio；Modbus TCP、S7 双端；无新增依赖。
- 分支：`codex/s7-workbench-baseline` → `16c12ab`（原有工作，38 项测试）；`codex/project-workspace-r1` → `402993b`（项目文件，69 项测试）；当前 `codex/r2-point-tables` 已完成 R2，独立提交保存。
- R1：独立项目与本地模式，项目文件原子保存、外部修改检测、旧配置复制导入；打开不自动联网。切换项目前需关闭工作窗口。日志仍在本机。
- R2：已完成命名点表、项目 v2/v1 迁移、JSON/CSV 校验预览、一次性整表读取及原始值写入；界面在项目模式客户端「点表」页。118 项回归及低 GC 阈值压力检查通过，详见 [开发文档](requirements/R2-point-tables.md)。
- R3 起才实现多组轮询、调度队列和完整采样质量。不得在 R2 中扩大到多连接、统一工作区或场景引擎。
- 下一项：从当前已提交的 R2 创建 `codex/r3-scheduling`，先读取 [R3 文档](requirements/R3-scheduling.md)。现有 Session.execute_table 是单次串行执行，R3 将统一手动请求与轮询调度；TableReadUpdate 已携带 table_id / point_id。PointDefinition 不含运行值，当前采样位于 PointTablePane 内存，需迁往独立 Sample 模型。
- 常用验证：`.venv/Scripts/python.exe -m pytest -q`、`git diff --check`。Qt 离屏环境没有默认字体时，截图检查需显式加载系统 msyh.ttc；产品沿用原有字体设置。
- Git：每个需求独立分支与提交，不自动合并或推送；任务结束保留干净工作区。原有改动已分别提交，不再存在混合暂存问题。
