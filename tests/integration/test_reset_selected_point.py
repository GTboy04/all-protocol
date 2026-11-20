import asyncio

import pytest
from PySide6.QtCore import QObject, Signal

from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import ModbusServerTarget, ServerPlan, Station
from all_protocol.adapters.modbus_rtu.server_model import RtuServerPlan
from all_protocol.adapters.s7.model import Area as S7Area, DataType, S7Target
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.adapters.opcua.model import UaTarget, UaType
from all_protocol.adapters.opcua.server_model import SIMULATOR_NAMESPACE, UaServerPlan, UaServerPoint
from all_protocol.application.server_contracts import ListenerState
from all_protocol.storage import ServerStorage, RtuServerStorage, S7ServerStorage, UaServerStorage
from all_protocol.ui.modbus_server_pane import ModbusServerPane
from all_protocol.ui.modbus_rtu_server_pane import ModbusRtuServerPane
from all_protocol.ui.s7_server_pane import S7ServerPane
from all_protocol.ui.opcua_server_pane import UaServerPane


@pytest.mark.parametrize("protocol", ("modbus", "rtu", "s7", "ua"))
def test_selected_reset_button_target_and_availability(protocol, tmp_path, qt_application):
    class Session:
        targets = []

        async def reset_point(self, target):
            self.targets.append(target)

    class Bridge(QObject):
        event = Signal(object)
        session = Session()

        def submit(self, coroutine):
            asyncio.run(coroutine)

    if protocol in ("modbus", "rtu"):
        station = Station(3).add_points(Area.HR, 100, 2, 7)
        plan = ServerPlan(stations=(station,)) if protocol == "modbus" else RtuServerPlan(stations=(station,))
        storage_type, pane_type = ((ServerStorage, ModbusServerPane) if protocol == "modbus"
                                   else (RtuServerStorage, ModbusRtuServerPane))
        target = ModbusServerTarget(3, Area.HR, 101)
    elif protocol == "s7":
        target = S7Target(S7Area.DB, 1, 2, DataType.WORD)
        plan = S7ServerPlan(points=(S7ServerPoint(S7Target(S7Area.DB, 1, 0, DataType.WORD)),
                                    S7ServerPoint(target, 7)))
        storage_type, pane_type = S7ServerStorage, S7ServerPane
    else:
        target = UaTarget(SIMULATOR_NAMESPACE, "s", "Second", UaType.Int32)
        plan = UaServerPlan(points=(UaServerPoint(UaTarget(SIMULATOR_NAMESPACE, "s", "First", UaType.Int32)),
                                   UaServerPoint(target, 7)), security="None/None")
        storage_type, pane_type = UaServerStorage, UaServerPane
    storage = storage_type(tmp_path)
    storage.save_plan(plan)
    bridge = Bridge()
    pane = pane_type(bridge, storage)
    try:
        pane.point_table.selectRow(1)
        assert not pane.reset_selected.isEnabled()
        pane.state = ListenerState.LISTENING
        if protocol == "modbus":
            pane._active_stations = frozenset({3})
        pane._refresh()
        assert pane.reset_selected.isEnabled()
        pane.show_workspace_values(False)
        assert pane.reset_selected.isHidden()
        pane.show_workspace_values(True)
        assert not pane.reset_selected.isHidden()
        pane.reset_selected.click()
        assert bridge.session.targets == [target]
        pane.point_table.setCurrentIndex(pane.points.index(-1, -1))
        pane._refresh()
        assert not pane.reset_selected.isEnabled()
        if protocol == "modbus":
            pane.point_table.selectRow(1)
            pane._active_stations = frozenset()
            pane._refresh()
            assert not pane.reset_selected.isEnabled()
    finally:
        pane.close()
        pane.deleteLater()
