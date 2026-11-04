# 工业协议调试台

Windows 桌面工业协议调试工具。启动时可选择客户端或服务端。客户端通过 Modbus TCP 访问设备或网关，支持四区读取、HR 和 Coil 写入、定时读取、请求日志及 TX/RX 字节查看。

## 开发启动

在项目目录使用 uv 安装锁定依赖并启动。

```powershell
uv sync --locked --python 3.12
uv run --locked python -m all_protocol
```

填写主机、端口和超时后连接，再按设备文档选择 Unit ID、数据区、起始偏移及数量。起始偏移从 0 开始。读取结果中的数值可用于核对写入。需要查看报文时，选中底部日志中的对应请求。

## 最近连接和请求日志

发起连接时，软件按协议和设备地址保存配置。执行读取、写入或启动轮询时，会更新该设备最近使用的点位和轮询间隔。重启后自动填入最近使用的设备，也可从「最近连接」切换到其他设备，点击「连接」后才会发起连接。

断开设备后，可在「最近连接」中选中一条记录，点击「删除记录…」并确认。之后重新连接该设备时会重新保存配置。

Windows 本地数据保存在 `%LOCALAPPDATA%\AllProtocol`。`connections.json` 保存连接配置，`requests.jsonl` 保存请求结果、错误详情及 TX/RX 报文。请求日志列表显示最近 500 条。追加日志将超过 5 MB（5,242,880 字节）时，清除旧日志并从当前这条重新记录。日志清理独立于连接历史。

## 验证与打包

```powershell
uv run --locked pytest -q
uv run --locked --group release pyinstaller --noconfirm AllProtocol.spec
```

测试会在本机临时端口启动独立 TCP 测试服务。发布时复制或压缩整个 `dist/AllProtocol` 目录，解压后运行其中的 `AllProtocol.exe`。目录内附带 Python 运行时和 Qt 依赖。打包配置让 Qt 使用 Windows 自带的 ICU，避免构建机上其他软件的同名 DLL 进入发布包。

新增协议时，实现 `application/contracts.py` 中的会话契约，为协议提供连接配置和目标表单，并在 `__main__.py` 中登记。连接生命周期、轮询与请求日志由通用会话层提供。表单可实现 `profile_fields()` 和 `restore_profile()` 以接入最近连接。 [V1 开发文档](docs/v1-development.md)

## Modbus TCP 服务端

1. 在首页点击「进入服务端」，新增从站并为 HR、IR、Coil 或 DI 添加点位。偏移从 0 开始。
2. 点击「启动服务」。本机默认监听 `127.0.0.1:1502`，可再打开一个实例进入客户端，用相同端口、Unit ID 和偏移访问点位。局域网联调时将监听地址改为本机网卡 IP。
3. 监听中可在点位表修改当前值来模拟输入。客户端写入 HR 或 Coil 后，当前值会同步刷新。选中请求日志即可查看 RX 和 TX 报文。

「保存方案」将监听参数、从站和点位初始值保存到 `%LOCALAPPDATA%\AllProtocol\modbus-tcp-server.json`。每次启动监听从初始值创建当前值。服务端日志独立保存在 `server-requests.jsonl`，沿用 5 MB 清理和最近 500 条的展示规则。

[服务端设计文档](docs/modbus-tcp-server-design.md) · [角色选择与服务端界面草图](docs/modbus-tcp-server-draft.html)
