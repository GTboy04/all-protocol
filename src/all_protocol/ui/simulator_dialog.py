"""Create independent listeners; shared Modbus buses use the existing station editor."""
from dataclasses import replace
from uuid import uuid4

from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit

from ..adapters.modbus_tcp.server_model import ServerPlan
from ..adapters.modbus_rtu.server_model import RtuServerPlan
from ..adapters.s7.server_model import S7ServerPlan
from ..adapters.opcua.server_model import UaServerPlan
from ..project import ProjectSimulator, SERVER_NAMES
from .widgets import EditDialog, spinbox


PLANS = {"modbus-tcp": ServerPlan, "modbus-rtu": RtuServerPlan, "s7": S7ServerPlan, "opcua": UaServerPlan}
HINTS = {
    "s7": "每台 PLC 使用独立 IP / 端口，点位、当前值和启停互相独立。客户端须连接对应端点。",
    "modbus-tcp": "模拟独立 TCP 设备时添加服务；同一网关下多台设备可在原服务中「新增从站」，用 Unit ID 区分。",
    "modbus-rtu": "同一串口总线的多台设备应在原服务中「新增从站」，用站号区分。只有另一条总线才需添加服务，串口不能重复占用。",
    "opcua": "独立 OPC UA 服务使用不同端口，各自拥有独立节点和订阅。模拟端仅支持本机回环地址；启动前须选择模拟模式并配置节点。",
}


def add_simulator_dialog(parent, servers, added, source=None, selected_protocol=None):
    items = servers.simulator_snapshot()
    protocol = QComboBox()
    for key in servers.available_protocols:
        protocol.addItem(SERVER_NAMES[key], key)
    name, host = QLineEdit(), QLineEdit()
    port = spinbox(1, 65535, 1102)
    hint = QLabel()
    hint.setWordWrap(True)
    hint.setMaximumWidth(490)
    name.setAccessibleName("模拟设备名称")
    host.setAccessibleName("模拟设备监听地址或串口")
    port.setAccessibleName("模拟设备端口")

    def configure():
        key = protocol.currentData()
        plan = source.plan if source else PLANS[key]()
        stem = source.name + " 副本" if source else SERVER_NAMES[key]
        index, names = 1, {item.name for item in items}
        candidate = stem
        while candidate in names:
            index += 1
            candidate = f"{stem} {index}"
        name.setText(candidate)
        rtu = key == "modbus-rtu"
        host_label.setText("串口" if rtu else "监听地址")
        host.setText(plan.serial_port if rtu else plan.host)
        port.setEnabled(not rtu)
        if rtu:
            used = {item.plan.serial_port.upper() for item in items if item.protocol == key}
            number = 1
            while f"COM{number}" in used:
                number += 1
            host.setText(f"COM{number}")
        else:
            used = {item.plan.port for item in items if item.protocol != "modbus-rtu"}
            value = plan.port
            while value in used and value < 65535:
                value += 1
            port.setValue(value)
        hint.setText(HINTS[key])

    def commit():
        key = protocol.currentData()
        title = name.text().strip()
        if not title or len(title) > 200:
            raise ValueError("设备名称必须为 1–200 字符")
        plan = source.plan if source else PLANS[key]()
        plan = replace(plan, **({"serial_port": host.text().strip()} if key == "modbus-rtu" else
                                {"host": host.text().strip(), "port": port.value()}))
        plan.validate()
        for item in servers.simulator_snapshot():
            other = item.plan
            if key == "modbus-rtu" and item.protocol == key and other.serial_port.casefold() == plan.serial_port.casefold():
                raise ValueError(f"串口已配置给「{item.name}」；同一总线请在该服务中新增从站")
            if key != "modbus-rtu" and item.protocol != "modbus-rtu" and other.port == plan.port:
                from ipaddress import ip_address
                left, right = ip_address(other.host), ip_address(plan.host)
                if left == right or left.version == right.version and (left.is_unspecified or right.is_unspecified):
                    raise ValueError(f"监听地址与「{item.name}」冲突，请修改 IP 或端口")
        item = ProjectSimulator(str(uuid4()), title, key, plan)
        servers.add_simulator(item)
        added(item.id)

    dialog = EditDialog(parent, "复制模拟设备" if source else "添加模拟设备", commit)
    dialog.setMinimumWidth(530)
    dialog.form.addRow("设备名称", name)
    dialog.form.addRow("协议", protocol)
    host_label = QLabel("监听地址")
    dialog.form.addRow(host_label, host)
    dialog.form.addRow("端口", port)
    dialog.form.addRow(hint)
    if source:
        protocol.setCurrentIndex(protocol.findData(source.protocol))
        protocol.setEnabled(False)
    elif selected_protocol is not None:
        protocol.setCurrentIndex(protocol.findData(selected_protocol))
    protocol.currentIndexChanged.connect(configure)
    configure()
    dialog.exec()
