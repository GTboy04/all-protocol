# R12 · OPC UA 预览版

2026-11-15，分支 `codex/r12-opc-ua`。承接当前未提交的 R11 软件基线和点位排序改动，不合并或推送。

## 已实现

- `opcua` 客户端注册：opc.tcp 端点发现、逐层浏览 HierarchicalReferences、Objects 入口，浏览显示节点类别、内置标量类型和当前用户写权限。双击对象继续浏览，双击变量填入快速读写目标。浏览支持续页，最多一层 2000 个引用；不跨服务端跳转。
- 内置标量 Boolean、SByte、Byte、Int16/UInt16、Int32/UInt32、Int64/UInt64、Float、Double、String。数组、结构体、自定义类型、方法、历史访问和 PubSub 不在本版范围。写入前校验类型与 UserAccessLevel，并沿用确认、编码后值、自动回读、容差和独立核对。
- 使用命名空间 URI 和 `i/s/g/b` 类型化标识符保存目标，每次连接从 NamespaceArray 解析索引，不持久化会话索引。点表 JSON/CSV、SQLite 项目、本地写入证据和报告可往返。String 写入完整保留空格、逗号、换行和空字符串。
- DataValue 保留原始 StatusCode、Good/Uncertain/Bad、源时间戳与服务端时间戳。只有 Good 更新最后有效值；Uncertain/Bad 保留旧值并单独显示。质量异常的回读为「无法验证」，不能判为一致。原生元数据写入本机日志和报告 JSON，报告 HTML/CSV 的详情也保留状态与时间戳。
- 数据变更订阅：按项目点表创建 MonitoredItem，使用表周期作为请求采样/发布间隔，可选队列长度 1–1000，显示服务端修订参数及 StatusCode Overflow 位。与轮询互相切换，最多 64 张表、每表 1000 项。过期、断开、原生质量沿用采样规则。

订阅的过期时间衡量最近一次值通知的年龄。变量长期不变时也可能显示「过期」，该标记不等同于会话断开；可根据业务需要调整点表过期时间，或改用周期读取确认当前值。
- 每次订阅有独立运行身份及会话代次，停止/断线时先失效身份，再删除订阅和关闭传输，旧通知不能复活数据。意外断线后由用户重新连接；连接参数相同时重建原订阅。主动断开不恢复，修改或删除表会撤销其恢复记录；不自动重放写入，不持久化运行订阅。
- 本机 OPC UA 模拟端：稳定的 `urn:allprotocol:simulator` 命名空间，Objects 下 `s=AllProtocol` 根对象，可配置最多 1000 个标量节点、初始值与读写权限，支持运行值修改和场景推送、手动接管/释放。必须显式启用「仅本机无加密匿名模拟」，只允许回环 IP。模拟端首版不提供加密监听和故障注入。

## 安全连接和本机配置

客户端默认选择 Basic256Sha256 / SignAndEncrypt，身份为匿名用户；服务端决定该用户的节点权限。无加密匿名连接必须显式选择，不会从安全模式自动降级。暂不提供用户名/密码身份。

「本机证书…」可生成客户端证书和 PEM 私钥，或引用已有文件；同时选择已通过可信渠道核对的服务端证书。客户端固定信任该证书，检查证书有效期、应用 URI、密钥用途、服务端用途及端点 IP/DNS。只接受精确主机身份，未启用通配符或 CA/CRL 管理。更换服务端证书时须重新核对和配置；发现端点不会自动建立信任。

本机配置存于 `%LOCALAPPDATA%\AllProtocol\opcua`，包含客户端证书、私钥、已信任服务端证书的路径和应用 URI。生成的私钥使用本机用户目录访问权限保护，不导入项目或报告。项目仅保存不透明配置 ID；换机后需重新建立本机证书配置，并将客户端公钥证书加入目标服务端信任列表。

## 格式与诊断

含 OPC UA 设备或非默认模拟方案的项目使用配置 v5 / SQLite user_version 3，旧版应用会拒绝。没有 OPC UA 配置时继续写 v4；读取 v1–v4 会补未启用的默认 UA 方案。私钥、密码及证书路径字段不能进入 OPC UA 项目设备配置。

CSV 使用 `name,namespace_uri,identifier_type,identifier,data_type` 地址列和既有名称、单位、倍率、偏置、精度、说明字段。工程值换算只用于数值，String 原样显示。

客户端捕获真实 TCP TX/RX，诊断解释 UA TCP 类型、分块、长度和通道 ID，不解密安全载荷。服务名、节点、耗时、StatusCode 和时间戳来自结构化操作记录。模拟端使用 asyncua 的外部 Read/Write 回调记录服务与状态码；该回调不提供远端地址及原始字节，因此不伪造报文或精确连接身份。订阅周期不计作网络请求往返耗时。

写入无自动重试。已发出 WriteRequest 后超时或断开为「写入结果未知」，不会自动回读或重发；预检、权限或类型校验失败为「写入未发送」；明确的服务端拒绝单独记录。可以由用户独立读取核对结果。

## 验证与边界

协议库锁定 `asyncua 1.1.8`；同步 `python-opcua 0.98.13` 仅用于开发互通测试，不打入产品。两者属于 FreeOpcUa 家族的不同同步/异步协议实现，验证不能替代独立厂商设备验收。

自动化覆盖独立服务端的发现、浏览、明文及加密读写、证书错误/过期/主机不符、未知和只读节点、类型不符、原生数据质量、写入超时且仅发送一次、回读证据、订阅停止/重连/旧通知/溢出、模拟端与独立客户端互通及场景推送、JSON/CSV/SQLite/报告、取消握手与资源关闭，以及桌面浏览、订阅和项目重开。

最终结果：全量 **376 通过、3 跳过**，其中新增 OPC UA **28 项**；Windows PyInstaller 构建通过。最终产物离屏启动后保持运行 6 秒，未发现导入异常，随后关闭本次测试进程。此检查仅覆盖基础启动，不替代打包版现场协议互通。原始结果位于忽略目录 `build/r12-pytest.log`、`build/r12-build.log`、`build/r12-smoke.log`。桌面测试使用 Windows 系统字体完成离屏布局检查，不修改产品字体。

验证命令：

```powershell
uv run --locked pytest -q --tb=short
uv run --locked --group release pyinstaller --noconfirm AllProtocol.spec
```

仍需现场验证：至少一个独立厂商 OPC UA 服务端的证书信任流程、实际 PLC 节点、网络中断恢复、长期订阅及 Windows 打包版互通。当前标记为预览版；R11 的真实串口验收边界保持原记录。

## 参考

- [asyncua 客户端 API](https://opcua-asyncio.readthedocs.io/en/latest/api/asyncua.client.html)
- [asyncua 节点与订阅 API](https://opcua-asyncio.readthedocs.io/en/latest/api/asyncua.common.html)
- [OPC UA DataValue](https://reference.opcfoundation.org/specs/OPC-10000-4/7.11)
- [独立同步服务端加密示例](https://github.com/FreeOpcUa/python-opcua/blob/master/examples/server-with-encryption.py)
