from .adapters.modbus_rtu.server_adapter import RtuServerAdapter
from .storage import RtuServerStorage
from .ui.modbus_rtu_server_pane import ModbusRtuServerPane
from .adapters.opcua.server_adapter import UaServerAdapter
from .storage import UaServerStorage
from .ui.opcua_server_pane import UaServerPane
from .adapters.modbus_tcp.server_adapter import ModbusServerAdapter
from .adapters.s7.server_adapter import S7ServerAdapter
from .application.server_contracts import ServerCapability, ServerRegistration
from .storage import S7ServerStorage, ServerStorage
from .ui.modbus_server_pane import ModbusServerPane
from .ui.s7_server_pane import S7ServerPane
from .ui.server_workbench import DesktopServerProtocol


_RUNTIME_VALUES = frozenset({ServerCapability.READ_CURRENT, ServerCapability.WRITE_CURRENT})

SERVER_PROTOCOLS = (
    DesktopServerProtocol(
        ServerRegistration("modbus-tcp", "Modbus TCP", ModbusServerAdapter, _RUNTIME_VALUES,
                           lambda plan: plan.simulation_points(configured_only=True)),
        ModbusServerPane, ServerStorage, project_field="modbus",
    ),
    DesktopServerProtocol(
        ServerRegistration("s7", "Siemens S7", S7ServerAdapter, _RUNTIME_VALUES,
                           lambda plan: plan.simulation_points(configured_only=True)),
        S7ServerPane, S7ServerStorage, project_field="s7",
    ),
    DesktopServerProtocol(
        ServerRegistration("modbus-rtu", "Modbus RTU", RtuServerAdapter, _RUNTIME_VALUES,
                           lambda plan: plan.simulation_points(configured_only=True)),
        ModbusRtuServerPane, RtuServerStorage, project_field="rtu",
    ),
    DesktopServerProtocol(
        ServerRegistration("opcua", "OPC UA（本机模拟）", UaServerAdapter, _RUNTIME_VALUES,
                           lambda plan: plan.simulation_points(configured_only=True)),
        UaServerPane, UaServerStorage, project_field="ua",
    ),
)
