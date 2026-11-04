import asyncio
import socket
import struct
from dataclasses import replace
from datetime import datetime

import pytest

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.adapters.modbus_tcp.server import ServerRequest, ValuesChanged
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.application.contracts import ClientEvents
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


def test_role_window_server_can_start_and_return_home(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
    from test_desktop import wait_for

    from all_protocol.ui.role_window import RoleWindow

    app = QApplication.instance() or QApplication([])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    station = Station(1).add_points(Area.HR, 0, 1, 1200)
    ServerStorage(tmp_path).save_plan(ServerPlan(port=port, stations=(station,)))
    role = RoleWindow((), tmp_path)
    try:
        role.show()
        next(button for button in role.findChildren(QPushButton) if button.text() == "进入服务端").click()
        window = role._workbench
        assert not role.isVisible()
        assert window.status.text() == "已停止"
        assert window.points.index(0, 4).data() == "—"
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
        assert window.dirty.text() == "已保存"
        window.listen.click()
        wait_for(lambda: window.status.text() == "已停止")
        assert window.points.index(0, 4).data() == "—"
        window.host.setText("0.0.0.0")
        home = next(button for button in window.findChildren(QPushButton) if button.text() == "返回首页")

        def choose(label):
            prompt = app.activeModalWidget()
            assert isinstance(prompt, QMessageBox)
            next(button for button in prompt.buttons() if button.text() == label).click()

        QTimer.singleShot(0, lambda: choose("取消"))
        home.click()
        assert window.isVisible()
        QTimer.singleShot(0, lambda: choose("放弃修改"))
        home.click()
        wait_for(role.isVisible)
        assert ServerStorage(tmp_path).load_plan().host == "127.0.0.1"
    finally:
        role.join()
        app.processEvents()
        role.close()
