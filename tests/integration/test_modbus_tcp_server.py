import asyncio
import json
import socket
import struct
from dataclasses import replace
from datetime import datetime

import pytest

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.adapters.modbus_tcp.server import ServerRequest, ValuesChanged
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.values import DataType, RegisterOrder
from all_protocol.application.contracts import ClientEvents, ProtocolRegistration
from all_protocol.application.server_session import ListenerState, ListenerUpdate, ServerSession
from all_protocol.storage import ServerStorage


def test_server_real_tcp_requests_and_restart():
    async def exercise():
        events = []
        session = ServerSession(events.append)
        first = Station(1, points=(
            ServerPoint(Area.HR, 0, 1200), ServerPoint(Area.HR, 1, 7),
            ServerPoint(Area.HR, 65535, 65535), ServerPoint(Area.IR, 0, 23),
            ServerPoint(Area.COIL, 65535, 1), ServerPoint(Area.DI, 0, 0),
        )).add_points(Area.COIL, 3, 9, 0)
        second = Station(255).add_points(Area.HR, 0, 1, 900)
        async with await asyncio.start_server(lambda *_: None, "127.0.0.1", 0) as occupied:
            port = occupied.sockets[0].getsockname()[1]
            plan = ServerPlan(port=port, stations=(first, second))
            await session.start(plan)
            assert events[-1].state == ListenerState.STOPPED
            assert events[-1].error

        def adu(tid, unit, pdu):
            return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu

        async def receive(reader):
            header = await asyncio.wait_for(reader.readexactly(7), 2)
            length = int.from_bytes(header[4:6], "big")
            return header + await asyncio.wait_for(reader.readexactly(length - 1), 2)

        await session.start(plan)
        assert events[-1] == ListenerUpdate(ListenerState.LISTENING)
        reader, writer = await asyncio.open_connection(plan.host, plan.port)
        try:
            async def check(unit, request, expected):
                tid = 42
                rx = adu(tid, unit, bytes.fromhex(request))
                tx = adu(tid, unit, bytes.fromhex(expected))
                writer.write(rx)
                await writer.drain()
                assert await receive(reader) == tx
                entry = events[-1]
                assert (entry.unit_id, entry.rx, entry.tx) == (unit, rx, tx)
                assert entry.peer.startswith("127.0.0.1:")

            # Send one request in fragments, followed by a batch larger than a TCP ADU.
            rx = adu(1, 1, bytes.fromhex("03 0000 0001"))
            writer.write(rx[:5])
            await writer.drain()
            await asyncio.sleep(0.01)
            assert isinstance(events[-1], ListenerUpdate)
            writer.write(rx[5:] + b"".join(adu(tid, 255, rx[7:]) for tid in range(2, 52)))
            await writer.drain()
            assert await receive(reader) == adu(1, 1, bytes.fromhex("03 02 04b0"))
            for tid in range(2, 52):
                assert await receive(reader) == adu(tid, 255, bytes.fromhex("03 02 0384"))

            await check(1, "03 ffff 0001", "03 02 ffff")
            await check(1, "03 ffff 0002", "83 02")
            await check(1, "01 ffff 0001", "01 01 01")
            await check(1, "05 ffff 0000", "05 ffff 0000")
            await check(1, "0f 0003 0009 02 55 01", "0f 0003 0009")
            await check(1, "01 0003 0009", "01 02 55 01")
            await check(1, "01 0002 0002", "81 02")
            await check(1, "04 0000 0001", "04 02 0017")
            await check(1, "02 0000 0001", "02 01 00")
            await session.set_current(1, Area.IR, 0, 42)
            await session.set_current(1, Area.DI, 0, 1)
            await check(1, "04 0000 0001", "04 02 002a")
            await check(1, "02 0000 0001", "02 01 01")
            await check(1, "06 0000 0514", "06 0000 0514")
            assert events[-2] == ValuesChanged(1, Area.HR, 0, (1300,))
            await check(1, "10 0000 0002 04 000b 000c", "10 0000 0002")
            await check(1, "10 0000 0003 06 0001 0002 0003", "90 02")
            await check(1, "03 0000 0002", "03 04 000b 000c")
            await check(255, "03 0000 0001", "03 02 0384")
            await check(0, "06 0000 0000", "86 0b")
            await check(2, "03 0000 0001", "83 0b")
            await check(1, "16 0000 ffff 0000", "96 01")
            await check(1, "65", "e5 01")
            await check(1, "05 ffff 0001", "85 03")
            await check(1, "10 0000 0002 02 0001", "90 03")

            other_reader, other_writer = await asyncio.open_connection(plan.host, plan.port)
            try:
                await asyncio.wait_for(session.stop(), 2)
                assert await asyncio.wait_for(reader.read(), 2) == b""
                assert await asyncio.wait_for(other_reader.read(), 2) == b""
            finally:
                other_writer.close()
                await other_writer.wait_closed()
        finally:
            writer.close()
            await writer.wait_closed()
            await session.stop()

        await session.start(plan)
        client = ModbusTcpClient(ModbusTcpConfig(plan.host, plan.port), ClientEvents(lambda *_: None, lambda *_: None))
        try:
            await client.connect()
            target = ModbusTarget(1, Area.HR, 0, 1)
            assert (await client.read(target))[0].value == 1200
            await client.write(target, (1300,))
            assert (await client.read(target))[0].value == 1300
        finally:
            await client.close()
            await session.stop()

    asyncio.run(exercise())


def test_server_plan_validation_and_storage(tmp_path, monkeypatch):
    storage = ServerStorage(tmp_path)
    station = Station(1, "模拟仪表").add_points(Area.HR, 2, 2, 100)
    with pytest.raises(ValueError, match="HR / 2"):
        station.add_points(Area.HR, 0, 3, 200)
    with pytest.raises(ValueError, match="65536"):
        station.add_points(Area.HR, 65535, 2, 100)
    plan = ServerPlan(stations=(station,))
    with pytest.raises(ValueError, match="Unit ID"):
        replace(plan, stations=(station, station)).validate()
    with pytest.raises(ValueError, match="至少"):
        ServerPlan().validate(starting=True)
    storage.save_plan(plan)
    assert storage.load_plan() == plan
    storage.plan_path.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        storage.load_plan()
    assert storage.plan_path.read_bytes() == b"invalid"
    storage.save_plan(plan)
    request = ServerRequest(datetime.now(), "127.0.0.1:52000", 1, 3, 2, 1, "成功", b"rx", b"tx")
    assert not storage.append_request(request)
    monkeypatch.setattr("all_protocol.storage.LOG_LIMIT_BYTES", storage.journal_path.stat().st_size + 1)
    latest = replace(request, rx=b"r2", tx=b"t2")
    assert storage.append_request(latest)
    assert storage.recent_requests() == (latest,)
    assert storage.load_plan() == plan


def test_typed_points_batch_overlap_and_legacy_plan(tmp_path):
    storage = ServerStorage(tmp_path)
    storage.plan_path.write_text(json.dumps({
        "schema_version": 1, "protocol": "modbus-tcp", "host": "127.0.0.1", "port": 1502,
        "stations": [{"unit_id": 1, "name": "", "points": [
            {"area": "HR", "offset": 0, "initial": 65535, "name": ""},
        ]}],
    }), encoding="utf-8")
    plan = storage.load_plan()
    station = plan.stations[0]
    assert station.points[0].value_type == DataType.UINT16
    station = station.add_points(Area.HR, 1, 2, 2**64 - 1, data_type=DataType.UINT64)
    assert [point.offset for point in station.points] == [0, 1, 5]
    with pytest.raises(ValueError, match="HR / 4"):
        station.add_points(Area.HR, 4, 1, 0)
    with pytest.raises(ValueError, match="65536"):
        station.add_points(Area.IR, 65535, 1, 1, data_type=DataType.INT32)
    with pytest.raises(ValueError, match="范围"):
        station.add_points(Area.HR, 9, 1, 2**64, data_type=DataType.UINT64)
    with pytest.raises(ValueError, match="不支持"):
        station.add_points(Area.COIL, 0, 1, 0, data_type=DataType.FLOAT32)
    plan = replace(plan, stations=(station,))
    storage.save_plan(plan)
    assert storage.load_plan() == plan


def test_role_window_runs_client_and_server_together(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFormLayout, QMessageBox, QPlainTextEdit, QPushButton
    from test_desktop import wait_for

    from all_protocol.ui.main_window import DesktopProtocol
    from all_protocol.ui.modbus_tcp_form import ModbusTcpForm
    from all_protocol.ui.role_window import RoleWindow

    app = QApplication.instance() or QApplication([])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    station = Station(1).add_points(Area.HR, 0, 1, 1200)
    ServerStorage(tmp_path).save_plan(ServerPlan(port=port, stations=(station,)))
    protocol = DesktopProtocol(
        ProtocolRegistration("modbus-tcp", "Modbus TCP", ModbusTcpClient, True), ModbusTcpForm)
    role = RoleWindow((protocol,), tmp_path)
    try:
        role.show()
        def button(parent, label):
            return next(item for item in parent.findChildren(QPushButton) if item.text() == label)

        button(role, "进入服务端").click()
        window, _ = role._workbenches["server"]
        assert role.isVisible()
        assert window.status.text() == "已停止"
        assert window.points.index(0, 4).data() == "—"

        def add_float():
            dialog = app.activeModalWidget()
            form = dialog.findChild(QFormLayout)
            fields = {
                form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget().text():
                form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
                for row in range(form.rowCount())
            }
            fields["偏移（0 基）"].setValue(1)
            fields["数据类型"].setCurrentText(DataType.FLOAT32.value)
            fields["寄存器顺序"].setCurrentText(RegisterOrder.SWAP_WORDS.label)
            fields["初始值"].setText("1.5")
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(0, add_float)
        window.add_point.click()
        assert window.points.index(1, 2).data() == "Float32"
        window.save.click()
        window.listen.click()
        wait_for(lambda: window.status.text() == "监听中")
        assert not window.add_point.isEnabled()
        assert window.points.index(0, 4).data() == "1200"
        window.point_table.selectRow(0)
        assert window.edit_value.isEnabled()
        frame = bytes.fromhex("0001 0000 0006 01 06 0000 0514")
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(frame)
            with sock.makefile("rb") as stream:
                assert stream.read(len(frame)) == frame
        wait_for(lambda: window.points.index(0, 4).data() == "1300")
        assert window.points.index(1, 0).data() == "1–2"

        button(window, "返回首页").click()
        assert window.status.text() == "监听中"
        button(role, "进入客户端").click()
        client, _ = role._workbenches["client"]
        button(role, "进入服务端").click()
        assert role._workbenches["server"][0] is window
        form = client._form
        form.connection.host.setText("127.0.0.1")
        form.connection.port.setValue(port)
        form.query.offset.setValue(1)
        form.query.count.setValue(1)
        form.query.data_type.setCurrentIndex(form.query.data_type.findData(DataType.FLOAT32.value))
        form.query.order.setCurrentIndex(form.query.order.findData(RegisterOrder.SWAP_WORDS.value))
        client.connection_button.click()
        wait_for(client.read_button.isEnabled)
        client.read_button.click()
        wait_for(lambda: client.results.rowCount() == 1 and client.read_button.isEnabled())
        assert client.results.items[0].value == 1.5
        assert client.results.items[0].hex_value == "0x00003FC0"

        def confirm_write():
            dialog = app.activeModalWidget()
            dialog.findChild(QPlainTextEdit).setPlainText("-2.5")
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(0, confirm_write)
        client.write_button.click()
        wait_for(lambda: window.points.index(1, 4).data() == "-2.5")
        frame = bytes.fromhex("0002 0000 0006 01 06 0002 4040")
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(frame)
            with sock.makefile("rb") as stream:
                assert stream.read(len(frame)) == frame
        wait_for(lambda: window.points.index(1, 4).data() == "3.0")
        client.close()
        wait_for(lambda: "client" not in role._workbenches)
        assert window.status.text() == "监听中"
        assert window.dirty.text() == "已保存"
        window.listen.click()
        wait_for(lambda: window.status.text() == "已停止")
        assert window.points.index(0, 4).data() == "—"
        window.host.setText("0.0.0.0")
        def choose(label):
            prompt = app.activeModalWidget()
            assert isinstance(prompt, QMessageBox)
            next(button for button in prompt.buttons() if button.text() == label).click()

        QTimer.singleShot(0, lambda: choose("取消"))
        window.close()
        assert window.isVisible()
        QTimer.singleShot(0, lambda: choose("放弃修改"))
        window.close()
        wait_for(lambda: "server" not in role._workbenches)
        assert ServerStorage(tmp_path).load_plan().host == "127.0.0.1"
    finally:
        role.join()
        app.processEvents()
        role.close()
