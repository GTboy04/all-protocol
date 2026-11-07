# 工业协议调试台

Windows 桌面工业协议调试工具。同一个实例可同时打开客户端和服务端。支持 Modbus TCP 和 Siemens S7（ISO-on-TCP），提供点位读写、定时读取、请求日志及 TX/RX 字节查看。两个协议的服务端可同时监听。

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

## 项目文件

首页提供「新建项目」「打开项目…」「保存项目」「另存为…」「从本地配置新建…」和「本地调试」。项目文件使用 `.allprotocol.json`，保存项目名称、多个客户端设备的连接/查询参数及轮询间隔，以及 Modbus TCP、S7 服务端方案。可复制给其他电脑并直接打开，打开时不会自动连接设备或启动监听。

在项目模式中，客户端通过「项目设备」切换已保存配置，仍只支持一个活动连接。填写主机和查询参数后即可返回首页保存，无需先连接；相同协议和端点更新原设备，不同端点添加为另一个设备。关闭客户端前也会将有效表单暂存到项目。非空但无效的配置需修正后再保存。

服务端的「应用到项目」仅暂存方案，仍需在首页「保存项目」写入文件。首页保存也会收集打开窗口中尚未应用的方案修改；模拟运行时的当前值不会覆盖方案初始值。关闭或切换项目时会提示处理未保存修改。

切换项目或返回本地调试前，请先关闭客户端和服务端窗口。「从本地配置新建…」复制最近连接与已保存方案，保留本地原文件。项目模式的日志仍保存在本机，不随项目文件导出。保存使用原子替换；当前文件被其他程序修改或删除时会拒绝覆盖，可另存为副本。

[开发需求与实施路线](docs/requirements/README.md) · [R1 项目开发文档](docs/requirements/R1-project.md)

## Modbus TCP 客户端

填写主机、端口和超时后连接，再按设备文档选择 Unit ID、数据区、起始偏移、数据类型、寄存器顺序及点位数量。起始偏移从 0 开始。HR 和 IR 支持 UInt16、Int16、UInt32、Int32、UInt64、Int64、Float32 和 Float64。16、32、64 位类型每点分别占用 1、2、4 个连续寄存器。Coil 和 DI 使用 Bool，每点占 1 位。读取结果同时显示解析值和原始十六进制数据。需要查看报文时，选中底部日志中的对应请求。

### 最近连接和请求日志

发起连接时，软件按协议和设备地址保存配置。执行读取、写入或启动轮询时，会更新该设备最近使用的点位和轮询间隔。重启后自动填入最近使用的设备，也可从「最近连接」切换到其他设备，点击「连接」后才会发起连接。

断开设备后，可在「最近连接」中选中一条记录，点击「删除记录…」并确认。之后重新连接该设备时会重新保存配置。

Windows 本地数据保存在 `%LOCALAPPDATA%\AllProtocol`。`connections.json` 保存连接配置，`requests.jsonl` 保存请求结果、错误详情及 TX/RX 报文。请求日志列表显示最近 500 条。追加日志将超过 5 MB（5,242,880 字节）时，清除旧日志并从当前这条重新记录。日志清理独立于连接历史。

## 服务端工作台

从首页进入「服务端」，使用顶部标签选择协议。每个标签独立启动或停止监听，切换标签时已开启的监听和客户端连接继续运行。关闭服务端工作台或首页后，应用会停止全部监听、关闭已接入的客户端连接并释放端口。 [多协议服务端设计](docs/multi-protocol-server-design.md)

### Modbus TCP 服务端

1. 在服务端选择「Modbus TCP」，新增从站并为 HR、IR、Coil 或 DI 添加点位。偏移从 0 开始。批量添加时按每点占用的寄存器数依次排布，弹窗会显示占用地址。
2. 点击「启动服务」。本机默认监听 `127.0.0.1:1502`。点击「返回首页」再进入客户端，用相同端口、Unit ID、偏移、类型和寄存器顺序访问点位。局域网联调时将监听地址改为本机网卡 IP。
3. 监听中可在点位表修改当前值来模拟输入。客户端写入 HR 或 Coil 后，当前值会同步刷新。选中请求日志即可查看 RX 和 TX 报文。

「保存方案」将监听参数、从站和点位初始值保存到 `%LOCALAPPDATA%\AllProtocol\modbus-tcp-server.json`。每次启动监听从初始值创建当前值。服务端日志独立保存在 `server-requests.jsonl`，沿用 5 MB 清理和最近 500 条的展示规则。

[服务端设计文档](docs/modbus-tcp-server-design.md) · [角色选择与服务端界面草图](docs/modbus-tcp-server-draft.html)

### Siemens S7 服务端

1. 在服务端选择「Siemens S7」，默认监听 `127.0.0.1:1102`，预置 `DB1.DBW0`（Word，初始值 0）。可添加 DB、M、I、Q 点位及 Bool、Byte、Int、Word、DInt、DWord、Real 类型。
2. 点击「启动服务」，回到客户端选择「Siemens S7（ISO-on-TCP）」，填入 `127.0.0.1:1102`、Rack 0、Slot 1，再读写 `DB1.DBW0`。第三方客户端可用经典 S7comm 的 Read Var 和 Write Var 访问模拟内存。
3. 监听时可通过「修改当前值」模拟 PLC 值变化，包括 I 输入区。I 区对网络客户端只读。选中日志可查看对应的 RX / TX 报文。

S7 方案保存在 `%LOCALAPPDATA%\AllProtocol\s7-server.json`，日志保存在 `s7-server-requests.jsonl`。重新启动监听时从方案初始值加载。

## Siemens S7 客户端

1. 进入客户端，选择「Siemens S7（ISO-on-TCP）」。填写 PLC 地址和端口（默认 102），Rack 和 Slot 按实际硬件配置填写。
2. 连接成功后选择 DB、M、I 或 Q 区，填写从 0 开始的字节偏移。选 DB 区时需填写 DB 号。比如读取 `DB1.DBX2.3`，选择 DB 1、字节偏移 2、Bool、位索引 3。选择 Int、Word、DInt、DWord 或 Real 时，相邻点分别以 2 或 4 字节递增。
3. 点击「读取」或「轮询」。DB、M、Q 可通过「写入…」输入并确认数值，I 区只读。选中请求日志可查看原始收发帧。写入结果未知时，可重新连接并读取点位核对。

使用经典 S7comm 的 S7-1200/1500 需要按 CPU 设置允许 PUT/GET，并为要访问的 DB 使用标准块访问。 [西门子 PUT/GET 手册](https://docs.tia.siemens.cloud/r/simatic_s7_1200_manual_collection_enus_20/communication/s7-communication/get-and-put-read-and-write-from-a-remote-cpu) · [S7 客户端设计](docs/s7-client-design.md)

## 开发与许可

新增协议时，实现 `application/contracts.py` 中的会话契约，为协议提供连接配置和目标表单，并在 `__main__.py` 中登记。连接生命周期、轮询与请求日志由通用会话层提供。表单可实现 `profile_fields()` 和 `restore_profile()` 以接入最近连接。 [V1 开发文档](docs/v1-development.md)

项目源码采用 [MIT 许可证](LICENSE)。公开分发 Windows 打包产物时，还需随包提供所含第三方依赖要求的许可和声明。
