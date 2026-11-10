# 协议扩展开发路线

2026-11-10 · 规划稿。基线为 `main` 已合入的功能测试修复 `2f075ee`，全量回归 245 项通过。R1–R10 已交付，当前产品支持 Modbus TCP 和经典 S7comm 双端。

| 优先级 | 协议 | 对应的联调场景 | 首版交付 | 立项条件 |
| --- | --- | --- | --- | --- |
| R11 | Modbus RTU | 调试同一条 RS-485 总线上的仪表、变频器与远程 I/O | 串口客户端和模拟服务端，接入点表、写入回读与报文诊断 | Windows 双端口链路和至少一台可安全读写的 RTU 设备 |
| R12 | OPC UA Client/Server | 浏览设备发布的变量，排查数据质量、权限及证书问题 | 先做客户端浏览与轮询，再加订阅和本地模拟服务 | Windows 打包可用，至少一个独立服务端可验证安全连接 |
| 候选 | EtherNet/IP（CIP） | 调试 Rockwell Logix 标签和机架路由 | 显式消息客户端，先做标量读取，再开放受控写入与回读 | 有明确目标机型、固件及可用的测试 PLC |
| 候选 | MQTT 5 | 联调网关的遥测上报、主题配置和命令消息 | 连接外部 Broker，查看订阅消息并手动发布 | 有明确的主题、载荷和应答约定 |

建议从 R11 开始。它增加串口接入，同时可以沿用 Modbus 点位语义、调度与写入证据链。R12 在用户能直接浏览变量之外，也会引入原生数据状态、订阅与证书管理，适合分批验收。

## R11 · Modbus RTU

### 交付范围

- 设备配置使用 `modbus-rtu` 协议键，字段包含 `serial_port`、`baudrate`、`parity`、`stopbits`、`timeout_ms`。固定 8 数据位，默认建议 `9600 / 8E1`，允许按设备手册调整。支持 Windows COM 端口枚举与手动输入。
- 客户端的一个项目设备代表一个 COM 口上的总线会话。Unit ID 放在快速操作的目标和点位中，同一个会话可轮询多个站号。首版处理单播地址 `1–247`，广播写入、Modbus ASCII、RTU over TCP 和网关转发留待独立需求。
- 客户端读取 HR、IR、Coil、DI，允许写入 HR 和 Coil。沿用 Modbus TCP 的零基偏移、寄存器类型和字序规则，以及既有写入确认和回读容差。服务端按站号和点位提供模拟数据，纳入项目场景、请求诊断及报告。
- 串口连接成功表示 COM 口已打开。站号可达性以该站号最近的有效响应为准，单个站号超时只影响该请求和对应采样。拔除适配器或驱动报错时，将整条总线标记为断开。

串口帧由站号、功能码及数据、CRC 组成，RTU 没有 MBAP 事务号。帧边界和校验按串行规范处理。 [Modbus 串行线路规范 V1.02](https://www.modbus.org/docs/Modbus_over_serial_line_V1_02.pdf)

### 实施顺序

1. **验证串口栈。** 用 `uv.lock` 中的 PyModbus 3.16.2 和 `pymodbus[serial]` 验证 `AsyncModbusSerialClient`、`ModbusSerialServer` 在 Windows 上的端口占用、收发、关闭和拔插行为。用不同的 COM 口连接客户端与模拟端，记录实际支持的串口参数、库回调提供的字节范围，以及 PyInstaller 产物启动结果。验证通过后再引入依赖并更新锁文件。 [PyModbus 串口安装说明](https://github.com/pymodbus-dev/pymodbus#install-with-pip)
2. **接入客户端。** 在 `adapters/modbus_rtu/` 实现配置和适配器，在 `__main__.py` 注册。把 TCP 与 RTU 共用的 Modbus 目标校验、值编解码和响应数据校验收敛到共同实现，保留 TCP 的 MBAP 检查。RTU 适配器通过库的帧解析及适配器检查，验证 CRC、站号、功能码、数据长度及写应答回显。使用现有 `SessionManager` 和设备队列，让一条总线同时只执行一个在途请求。客户端与模拟服务共用按规范化 COM 名称索引的进程内端口占用检查，打开失败或取消时也要释放。进程外占用交由操作系统报告。
3. **接入项目和报文。** `project.py` 按协议校验连接字段，区分 TCP 端口与 COM 口。`point_tables.py` 当前根据目标类型直接推断 `modbus-tcp`，改为由所在设备协议校验 Modbus 目标，并应用 RTU 站号限制。检查 `storage.py` 的写入证据往返和 `packet_decoder.py` 的 RTU 分支。点表 JSON 保留协议标记，CSV 沿用 Modbus 列并按导入的设备协议验证。报文记录实际捕获的 TX/RX 字节，标注不完整帧及 CRC 错误。
4. **接入模拟服务与报告。** 通过 `server_protocols.py` 注册适配器、界面和存储，按 `ServerAdapter` 的生命周期接入。复用 Modbus 的站号、点位和场景规则，串口参数另设方案。`project.py` 当前严格要求两种服务端方案，增加第三种方案时引入项目 v4，读取 v1–v3 时为 RTU 补未启用的默认方案，打开项目后由用户手动启动。`reporting.py` 按协议汇总服务端方案与证据，使 RTU 报告显示 COM 口与串口参数。旧版应用遇到 v4 应提示版本不兼容。

### 异常与验收

- **超时和迟到帧。** 底层请求禁用自动重试。写入发出后遇到超时、断开或无法关联的回包时，记录为「写入结果未知」。发送下一条请求前清理残留数据并遵循帧间隔。超时后继续轮询其他站号，丢弃站号不符的迟到帧。对同站号、同功能码且可能混淆的新请求，等经实机验证的最大响应窗口结束后再发送。无法确定安全等待时间时，暂停该站号并提示用户检查链路。用模拟端延迟应答验证这条路径。RTU 静默间隔只能区分帧边界，无法关联迟到应答。
- **自动化。** 覆盖合法帧、错误 CRC、截断、错误站号或功能码、迟到帧、写回显不符和未知写入。用可控串口替身验证同一 COM 排队、不同 COM 独立、端口冲突、打开失败和重复关闭。覆盖旧项目迁移、点表 JSON/CSV、写入证据及报告导出，并运行 `uv run --locked pytest -q`。
- **Windows 联调。** 使用虚拟串口对或物理串口链路验证双端读写，再用 USB 转 RS-485 接真实设备。对两个站号交替轮询，验证一个站号离线时另一个站号继续出数。另测端口被占用、拔插、写后回读、场景模拟和打包版启动。验收记录标注硬件型号、驱动、串口参数及测试证据。

## R12 · OPC UA Client/Server

1. **客户端浏览和轮询。** 先验证 `asyncua` 在 Python 3.12、Windows 和 PyInstaller 下能连接独立服务端。首版支持 `opc.tcp`、端点发现、按层浏览、标量读取和对可写变量的手动写入及回读。点表以命名空间 URI 和带类型的节点标识符保存目标，连接时解析服务器当前的命名空间索引。优先支持 JSON 点表，CSV 按节点地址字段另行设计。
2. **数据质量和安全。** 保留 `DataValue` 的 `StatusCode`、源时间戳与服务器时间戳。`Good` 才更新最后有效值，`Uncertain` 单独展示，`Bad` 显示原始状态且保留此前有效值。当前 `Point` / `Sample` 契约需要可选的协议元数据。首版验收一个明确的 `SignAndEncrypt` 安全策略和信任证书流程。无加密匿名连接由用户显式选择。密码和私钥留在本机凭据或证书存储，项目、日志和报告只保存必要的非敏感摘要。诊断先记录服务名、节点、耗时和状态码，按实际捕获能力提供报文。
3. **订阅和模拟端。** 在轮询通过验收后加入 `MonitoredItem` 数据变更通知。订阅事件需带会话身份和代次，断线重建后丢弃旧通知，并展示采样间隔、队列溢出及过期状态。模拟端以稳定命名空间和可配置标量节点提供读写，并将现有场景的变化推送给订阅客户端。方法调用、历史访问、复杂数据类型和 PubSub 单独评估。

验收包括使用独立服务端完成浏览、读写和回读，覆盖未知节点、只读节点、类型不匹配、状态码映射、命名空间索引变化和不受信任证书。订阅阶段增加断线恢复、旧事件隔离，以及模拟端向独立客户端推送变更的测试。 [OPC UA 服务规范](https://reference.opcfoundation.org/specs/OPC-10000-4/full) · [DataValue](https://reference.opcfoundation.org/specs/OPC-10000-4/7.11) · [证书管理](https://reference.opcfoundation.org/specs/OPC-10000-2/9.4.2) · [asyncua](https://github.com/FreeOpcUa/opcua-asyncio)

## 后续候选

- **EtherNet/IP（CIP）。** 有 Logix 实机后，以显式消息读取控制器和程序作用域的标量标签，记录路由路径、目标槽位及 CIP 通用和附加状态。先固定支持的机型和类型，再验证读写回读及目标不存在、路由错误等失败路径。周期 I/O 与设备模拟另行评估。 [ODVA EtherNet/IP 概览](https://www.odva.org/publication_download/ethernet-ip-technology-overview/) · [Rockwell Logix 标签访问指南](https://literature.rockwellautomation.com/idc/groups/literature/documents/pm/1756-pm020_-en-p.pdf)
- **MQTT 5。** 现场网关以遥测上报为主时，可提前安排。用独立消息视图显示主题、载荷、QoS、保留标记、到达时间及连接状态，先通过现成 Broker 验证重连、重订阅、保留消息及 QoS 1 重复投递。发布结果单独显示 Broker 应答。设备执行结果由业务应答主题和关联 ID 确认。完成主题监视后，再按约定把结构化载荷映射为点位。 [OASIS MQTT 5.0](https://docs.oasis-open.org/mqtt/mqtt/v5.0/mqtt-v5.0.html)

每个阶段从前置阶段已验收的代码开始。配置可往返、运行状态可诊断、资源可释放、真实设备或独立实现可互通后，再将对应协议标为已支持。
