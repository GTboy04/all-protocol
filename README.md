# 工业协议调试台

Windows 桌面工业协议调试工具。同一个实例可同时打开客户端和服务端。客户端通过 Modbus TCP 访问设备或网关，支持四区读取、HR 和 Coil 写入、定时读取、请求日志及 TX/RX 字节查看。

## 获取源码并准备环境

在 Windows 上安装 Git 和 uv，然后打开 PowerShell。项目使用 Python 3.12，可以直接通过 uv 安装。 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/)

```powershell
git clone https://github.com/GTboy04/all-protocol.git
cd all-protocol
uv python install 3.12
uv sync --locked --python 3.12
```

如果已经克隆了仓库，从仓库根目录执行后两条命令即可。uv 会创建 `.venv`，并按 `uv.lock` 安装依赖。下面的命令也在仓库根目录运行。

## 打包 Windows 应用

使用项目的 [AllProtocol.spec](AllProtocol.spec) 打包。命令会自动安装 `release` 依赖组中的 PyInstaller。`--noconfirm` 会直接替换已有的 `dist/AllProtocol`。

```powershell
uv run --locked --group release pyinstaller --noconfirm AllProtocol.spec
```

打包完成后，运行可执行文件并确认能进入角色选择页。

```powershell
.\dist\AllProtocol\AllProtocol.exe
```

运行目录是 `dist/AllProtocol`，其中包含 `AllProtocol.exe` 和存放 Python、Qt 等运行依赖的 `_internal`。复制到另一台 Windows 电脑时，请保留整个目录。可以用下面的命令生成 ZIP，接收者解压后运行其中的 `AllProtocol.exe`，无需另装 Python 或 uv。

```powershell
Compress-Archive -LiteralPath .\dist\AllProtocol -DestinationPath .\dist\AllProtocol-windows.zip -Force
```

## 从源码运行和测试

```powershell
uv run --locked python -m all_protocol
uv run --locked pytest -q
```

测试会在本机临时端口启动 TCP 服务，覆盖客户端、服务端和本地存储。

## Modbus TCP 客户端

填写主机、端口和超时后连接，再按设备文档选择 Unit ID、数据区、起始偏移、数据类型、寄存器顺序及点位数量。起始偏移从 0 开始。HR 和 IR 支持 UInt16、Int16、UInt32、Int32、UInt64、Int64、Float32 和 Float64。16、32、64 位类型每点分别占用 1、2、4 个连续寄存器。Coil 和 DI 使用 Bool，每点占 1 位。读取结果同时显示解析值和原始十六进制数据。需要查看报文时，选中底部日志中的对应请求。

### 最近连接和请求日志

发起连接时，软件按协议和设备地址保存配置。执行读取、写入或启动轮询时，会更新该设备最近使用的点位和轮询间隔。重启后自动填入最近使用的设备，也可从「最近连接」切换到其他设备，点击「连接」后才会发起连接。

断开设备后，可在「最近连接」中选中一条记录，点击「删除记录…」并确认。之后重新连接该设备时会重新保存配置。

Windows 本地数据保存在 `%LOCALAPPDATA%\AllProtocol`。`connections.json` 保存连接配置，`requests.jsonl` 保存请求结果、错误详情及 TX/RX 报文。请求日志列表显示最近 500 条。追加日志将超过 5 MB（5,242,880 字节）时，清除旧日志并从当前这条重新记录。日志清理独立于连接历史。

## Modbus TCP 服务端

1. 在首页点击「进入服务端」，新增从站并为 HR、IR、Coil 或 DI 添加点位。偏移从 0 开始。批量添加时按每点占用的寄存器数依次排布，弹窗会显示占用地址。
2. 点击「启动服务」。本机默认监听 `127.0.0.1:1502`。点击「返回首页」再进入客户端，用相同端口、Unit ID、偏移、类型和寄存器顺序访问点位。局域网联调时将监听地址改为本机网卡 IP。
3. 监听中可在点位表修改当前值来模拟输入。客户端写入 HR 或 Coil 后，当前值会同步刷新。选中请求日志即可查看 RX 和 TX 报文。

「保存方案」将监听参数、从站和点位初始值保存到 `%LOCALAPPDATA%\AllProtocol\modbus-tcp-server.json`。每次启动监听从初始值创建当前值。服务端日志独立保存在 `server-requests.jsonl`，沿用 5 MB 清理和最近 500 条的展示规则。

[服务端设计文档](docs/modbus-tcp-server-design.md) · [角色选择与服务端界面草图](docs/modbus-tcp-server-draft.html)

## 开发与许可

新增协议时，实现 `application/contracts.py` 中的会话契约，为协议提供连接配置和目标表单，并在 `__main__.py` 中登记。连接生命周期、轮询与请求日志由通用会话层提供。表单可实现 `profile_fields()` 和 `restore_profile()` 以接入最近连接。 [V1 开发文档](docs/v1-development.md)

项目源码采用 [MIT 许可证](LICENSE)。公开分发 Windows 打包产物时，还需随包提供所含第三方依赖要求的许可和声明。
