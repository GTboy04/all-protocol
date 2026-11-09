import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime
import json
import socket

import pytest
from snap7 import Client
from snap7.error import S7Error

from all_protocol.adapters.modbus_tcp.model import Area as ModbusArea
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, Station
from all_protocol.adapters.s7.client import S7Client
from all_protocol.adapters.s7.model import Area, DataType, S7Config, S7Target
from all_protocol.adapters.s7.server import S7Server
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.adapters.s7.wire import read_tpkt, tpkt
from all_protocol.application.contracts import ClientError, ClientEvents, ErrorKind
from all_protocol.application.server_contracts import LifecycleAction, RequestMetadata, ServerEvent, ServerLifecycle
from all_protocol.application.server_session import ServerSession
from all_protocol.server_protocols import SERVER_PROTOCOLS
from all_protocol.storage import S7ServerStorage, ServerStorage


def test_s7_server_typed_memory_and_bit_writes():
    async def run():
        with closing(socket.socket()) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        bit_1 = S7Target(Area.DB, 1, 0, DataType.BOOL, bit=1)
        bit_7 = replace(bit_1, bit=7)
        word = S7Target(Area.DB, 1, 1, DataType.WORD)
        input_point = S7Target(Area.I, 0, 0, DataType.BYTE)
        plan = S7ServerPlan(port=port, points=(
            S7ServerPoint(bit_1), S7ServerPoint(bit_7, True), S7ServerPoint(word, 123), S7ServerPoint(input_point, 8),
        ))
        plan.validate(starting=True)
        with pytest.raises(ValueError, match="重叠"):
            replace(plan, points=plan.points + (S7ServerPoint(S7Target(Area.DB, 1, 0, DataType.BYTE)),)).validate()
        events = []
        server = ServerSession(events.append, SERVER_PROTOCOLS[1].registration)
        await server.start(plan)
        client = S7Client(S7Config(plan.host, port), ClientEvents(lambda *_: None, lambda: None))
        try:
            await client.connect()
            assert (await client.read(bit_1))[0].value is False
            await client.write(bit_1, (True,))
            assert (await client.read(bit_7))[0].hex_value == "82"
            await client.write(word, (456,))
            assert (await client.read(word))[0].value == 456
            assert (await client.read(input_point))[0].value == 8
            assert await server.read_current(bit_1) is True
            assert await server.read_current(bit_7) is True
            await server.write_current(input_point, 9)
            assert await server.read_current(input_point) == 9
            assert (await client.read(input_point))[0].value == 9
            with pytest.raises(ClientError) as error:
                await client.read(replace(word, offset=2))
            assert error.value.kind == ErrorKind.PROTOCOL and error.value.code == 0x05
            requests = [event.payload for event in events if isinstance(event.payload, RequestMetadata)]
            assert requests and all(request.rx and request.tx for request in requests)
            assert any(dict(request.details)["target"] == "DB1 0.1" for request in requests)
        finally:
            await client.close()
            await server.stop()

    asyncio.run(run())


def test_s7_multi_item_padding_and_remote_input_protection():
    async def run():
        with closing(socket.socket()) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        plan = S7ServerPlan(port=port, points=(
            S7ServerPoint(S7Target(Area.DB, 1, 0, DataType.BYTE), 25),
            S7ServerPoint(S7Target(Area.I, 0, 0, DataType.BYTE), 7),
        ))
        journal = []
        server = S7Server(plan, lambda _: None, journal.append)
        await server.bind()
        reader, writer = await asyncio.open_connection(plan.host, port)

        async def exchange(request, expected):
            packet = tpkt(bytes.fromhex(request))
            writer.write(packet[:3])
            await writer.drain()
            writer.write(packet[3:])
            await writer.drain()
            reply = await asyncio.wait_for(read_tpkt(reader, 1024, lambda _: None), 2)
            assert reply == bytes.fromhex(expected)
            assert journal[-1].rx == packet and journal[-1].tx == tpkt(reply)

        try:
            await exchange("11 E0 0000 0002 00 C1020100 C2020101 C0010A",
                           "11 D0 0002 0001 00 C0010A C1020100 C2020101")
            await exchange("02 F0 80 3201 0000 0001 0008 0000 F000 0001 0001 01E0",
                           "02 F0 80 3203 0000 0001 0008 0000 0000 F000 0001 0001 01E0")
            items = "120A1002 0001 0001 84 000000 120A1002 0001 0000 81 000000"
            await exchange(f"02 F0 80 3201 0000 0002 001A 000B 0502 {items} 00040008 42 00 00040008 43",
                           "02 F0 80 3203 0000 0002 0002 0002 0000 0502 FF0A")
            await exchange(f"02 F0 80 3201 0000 0003 001A 0000 0402 {items}",
                           "02 F0 80 3203 0000 0003 0002 000B 0000 0402 FF040008 42 00 FF040008 07")
        finally:
            writer.close()
            await writer.wait_closed()
            await server.shutdown()

    asyncio.run(run())


def test_s7_server_plan_and_journal_round_trip(tmp_path):
    storage = S7ServerStorage(tmp_path)
    plan = S7ServerPlan()
    assert storage.load_plan() == plan
    storage.save_plan(plan)
    assert storage.load_plan() == plan
    now = datetime.now()
    legacy = {"time": now.isoformat(), "peer": "127.0.0.1:2000", "function": 4, "target": "DB1",
              "result": "成功", "rx": "01", "tx": "02"}
    storage.journal_path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    request = RequestMetadata(legacy["peer"], "成功", b"\x01", b"\x02", (("function", 4), ("target", "DB1")))
    start = ServerEvent("s7", "127.0.0.1:1102", now, ServerLifecycle(LifecycleAction.STARTED, "启动监听"))
    assert not storage.append_event(start)
    rows = storage.journal_path.read_text(encoding="utf-8").splitlines()
    latest = json.loads(rows[-1])
    assert latest["schema_version"] == 1 and latest["kind"] == "lifecycle"
    with storage.journal_path.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps({**latest, "schema_version": 2}) + "\n")
    assert storage.recent_events() == (start, ServerEvent("s7", "", now, request))
    assert not ServerStorage(tmp_path).plan_path.exists()
    assert not ServerStorage(tmp_path).journal_path.exists()


def test_switching_server_tabs_keeps_connections_and_app_close_releases_ports(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFormLayout
    from test_desktop import wait_for

    from all_protocol.ui.role_window import RoleWindow

    app = QApplication.instance() or QApplication([])
    with closing(socket.socket()) as first, closing(socket.socket()) as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        modbus_port, s7_port = first.getsockname()[1], second.getsockname()[1]
    station = Station(1).add_points(ModbusArea.HR, 0, 1, 1502)
    ServerStorage(tmp_path).save_plan(ServerPlan(port=modbus_port, stations=(station,)))
    S7ServerStorage(tmp_path).save_plan(S7ServerPlan(port=s7_port))
    role = RoleWindow((), tmp_path)
    s7_client = Client()
    try:
        role.show()
        role._open_server()
        workbench, _ = role._workbenches["server"]
        modbus, s7 = (workbench.tabs.widget(index) for index in range(2))
        modbus.listen.click()
        wait_for(lambda: modbus.status.text() == "监听中")
        with socket.create_connection(("127.0.0.1", modbus_port), timeout=2) as mb_client:
            with mb_client.makefile("rb") as mb_stream:
                def check_modbus():
                    mb_client.sendall(bytes.fromhex("002A 0000 0006 01 03 0000 0001"))
                    assert mb_stream.read(11) == bytes.fromhex("002A 0000 0005 01 03 02 05DE")

                check_modbus()
                workbench.tabs.setCurrentIndex(1)
                s7.port.setValue(modbus_port)
                s7.listen.click()
                wait_for(lambda: "启动失败" in s7.feedback.text())
                check_modbus()
                s7.port.setValue(s7_port)

                def accept_input_point():
                    dialog = app.activeModalWidget()
                    form = dialog.findChild(QFormLayout)
                    fields = {
                        form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget().text():
                        form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
                        for row in range(form.rowCount())
                    }
                    fields["数据区"].setCurrentText("I 输入")
                    fields["数据类型"].setCurrentText("Byte")
                    fields["初始值"].setText("14")
                    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

                QTimer.singleShot(0, accept_input_point)
                s7.add_point.click()
                assert s7.points.index(1, 0).data() == "IB0"
                s7.save.click()
                s7.listen.click()
                wait_for(lambda: "2 / 2" in workbench.overview.text())
                assert str(modbus_port) in workbench.tabs.tabText(0)
                assert str(s7_port) in workbench.tabs.tabText(1)
                s7_client.connect("127.0.0.1", 0, 1, tcp_port=s7_port)
                workbench.tabs.setCurrentIndex(0)
                s7_client.db_write(1, 0, bytearray.fromhex("1234"))
                assert s7_client.db_read(1, 0, 2) == bytes.fromhex("1234")
                check_modbus()
                workbench.tabs.setCurrentIndex(1)
                wait_for(lambda: s7.points.index(0, 4).data() == "4660")
                assert s7.points.index(1, 4).data() == "14"
                s7.listen.click()
                wait_for(lambda: s7.status.text() == "已停止")
                check_modbus()
                with pytest.raises(S7Error):
                    s7_client.db_read(1, 0, 2)
                s7_client.disconnect()
                s7.listen.click()
                wait_for(lambda: "2 / 2" in workbench.overview.text())
                s7_client.connect("127.0.0.1", 0, 1, tcp_port=s7_port)
                assert s7_client.db_read(1, 0, 2) == bytes.fromhex("0000")
                assert role.close()
                wait_for(lambda: "server" not in role._workbenches)
                for port in (modbus_port, s7_port):
                    with closing(socket.socket()) as probe:
                        probe.bind(("127.0.0.1", port))
                        probe.listen()
                assert mb_stream.read(1) == b""
                with pytest.raises(S7Error):
                    s7_client.db_read(1, 0, 2)
    finally:
        s7_client.disconnect()
        role.join()
        app.processEvents()
        role.close()
