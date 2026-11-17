import asyncio
import socket
import struct

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import ModbusServerTarget, ServerPlan, Station
from all_protocol.application.server_contracts import LifecycleAction, ListenerState, ListenerUpdate, RuntimeChange, ServerLifecycle, StationsChanged
from all_protocol.application.server_session import ServerSession
from all_protocol.server_protocols import SERVER_PROTOCOLS
from all_protocol.storage import ServerStorage
from all_protocol.ui.modbus_server_pane import ModbusServerPane


def plan():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return ServerPlan(port=port, stations=tuple(
        Station(unit).add_points(Area.HR, 0, 1, unit * 10) for unit in (1, 2)))


def test_station_control_over_same_tcp_connection():
    async def run():
        events = []
        session = ServerSession(events.append, SERVER_PROTOCOLS[0].registration)
        config = plan()
        await session.start(config, stations=frozenset({1}))
        reader, writer = await asyncio.open_connection(config.host, config.port)
        async def request(unit, pdu="03 0000 0001"):
            body = bytes.fromhex(pdu)
            writer.write(struct.pack(">HHHB", 1, 0, len(body) + 1, unit) + body)
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(7), 2)
            return await asyncio.wait_for(reader.readexactly(int.from_bytes(header[4:6], "big") - 1), 2)
        try:
            assert await request(1) == bytes.fromhex("03 02 000a")
            assert await request(2) == bytes.fromhex("83 0b")
            await session.set_station_active(2, True)
            assert await request(2, "06 0000 007b") == bytes.fromhex("06 0000 007b")
            assert await request(1, "06 0000 0037") == bytes.fromhex("06 0000 0037")
            await session.set_station_active(1, False)
            assert await request(1, "06 0000 0099") == bytes.fromhex("86 0b")
            assert await request(2) == bytes.fromhex("03 02 007b")
            # Reset every configured point without disconnecting this client.
            # A stopped station gets its reset before it serves requests again.
            await session.reset_points()
            assert await request(1) == bytes.fromhex("83 0b")
            assert await request(2) == bytes.fromhex("03 02 0014")
            assert any(isinstance(e.payload, ServerLifecycle) and e.payload.action == LifecycleAction.POINTS_RESET
                       for e in events)
            with pytest.raises(ValueError, match="从站未启动"):
                await session.write_current(ModbusServerTarget(1, Area.HR, 0), 99)
            await session.set_station_active(2, False)
            assert await request(2) == bytes.fromhex("83 0b")
            # The shared listener remains available even with all stations stopped.
            await session.set_station_active(1, True)
            await session.set_station_active(2, True)
            assert await request(1) == bytes.fromhex("03 02 000a")
            assert await request(2) == bytes.fromhex("03 02 0014")
            assert RuntimeChange(StationsChanged(frozenset({1, 2}))) in [e.payload for e in events]
        finally:
            writer.close()
            await writer.wait_closed()
            await session.stop()
        await session.start(config)
        try:
            assert await session.read_current(ModbusServerTarget(2, Area.HR, 0)) == 20
        finally:
            await session.stop()
    asyncio.run(run())


def test_individual_start_failure_can_retry():
    async def run():
        events = []
        session = ServerSession(events.append, SERVER_PROTOCOLS[0].registration)
        config = plan()
        occupied = await asyncio.start_server(lambda *_: None, config.host, config.port)
        try:
            await session.start(config, stations=frozenset({2}))
            assert events[-1].payload.state == ListenerState.STOPPED
            assert events[-1].payload.error
        finally:
            occupied.close()
            await occupied.wait_closed()
        await session.start(config, stations=frozenset({2}))
        try:
            assert events[-1].payload == ListenerUpdate(ListenerState.LISTENING)
            assert await session.read_current(ModbusServerTarget(2, Area.HR, 0)) == 20
            with pytest.raises(ValueError, match="从站未启动"):
                await session.read_current(ModbusServerTarget(1, Area.HR, 0))
        finally:
            await session.stop()
    asyncio.run(run())


def test_desktop_station_actions_and_state(tmp_path):
    class Bridge(QObject):
        event = Signal(object)
        def submit(self, coroutine):
            self.tasks.append(asyncio.create_task(coroutine))

    async def run():
        bridge = Bridge()
        bridge.tasks = []
        bridge.session = ServerSession(bridge.event.emit, SERVER_PROTOCOLS[0].registration)
        storage = ServerStorage(tmp_path)
        storage.save_plan(plan())
        pane = ModbusServerPane(bridge, storage)
        app = QApplication.instance()
        async def settle():
            await asyncio.gather(*bridge.tasks)
            bridge.tasks.clear()
            app.processEvents()
        try:
            assert not pane.reset_points.isEnabled()
            assert pane.start_station.isEnabled()
            assert not pane.stop_station.isEnabled()
            pane.start_station.click()
            await settle()
            assert pane.state == ListenerState.LISTENING
            assert pane.reset_points.isEnabled()
            target = ModbusServerTarget(1, Area.HR, 0)
            await bridge.session.write_current(target, 99)
            app.processEvents()
            assert pane.points.index(0, 4).data() == "99"
            pane.reset_points.click()
            await settle()
            assert pane.points.index(0, 4).data() == "10"
            assert pane.state == ListenerState.LISTENING
            assert not pane.start_station.isEnabled()
            assert pane.stop_station.isEnabled()
            pane.station_list.setCurrentRow(1)
            pane.point_table.selectRow(0)
            assert pane.points.index(0, 4).data() == "—"
            assert not pane.edit_value.isEnabled()
            pane.start_station.click()
            await settle()
            assert pane.points.index(0, 4).data() == "20"
            assert pane.edit_value.isEnabled()
            pane.stop_station.click()
            await settle()
            assert pane.points.index(0, 4).data() == "—"
            assert "已停止" in pane.station_list.item(1).text()
            pane.station_list.setCurrentRow(0)
            assert "运行中" in pane.station_list.item(0).text()
            assert pane.stop_station.isEnabled()
            pane.listen.click()
            await settle()
            assert pane.state == ListenerState.STOPPED
            assert not pane.reset_points.isEnabled()
            assert pane.start_station.isEnabled()
        finally:
            await bridge.session.stop()
            app.processEvents()
            pane.close()
            pane.deleteLater()
    asyncio.run(run())
