from dataclasses import dataclass, replace
from ..modbus_tcp.server_model import ServerPlan, Station
from .model import ModbusRtuConfig
from ...application.contracts import ClientError


@dataclass(frozen=True, slots=True)
class RtuServerPlan:
    serial_port: str = "COM2"
    baudrate: int = 9600
    parity: str = "E"
    stopbits: int = 1
    timeout_ms: int = 3000
    stations: tuple[Station, ...] = ()

    @property
    def config(self):
        return ModbusRtuConfig(self.serial_port, self.baudrate, self.parity, self.stopbits, self.timeout_ms)

    @property
    def endpoint(self):
        return self.config.endpoint

    def validate(self, *, starting=False):
        try:
            self.config.validate()
        except ClientError as error:
            raise ValueError(str(error)) from error
        ServerPlan(stations=self.stations).validate(starting=starting)
        if any(not 1 <= station.unit_id <= 247 for station in self.stations):
            raise ValueError("RTU 站号必须为 1–247")

    def simulation_points(self, *, configured_only=False):
        return ServerPlan(stations=self.stations).simulation_points(configured_only=configured_only)

    def copy_station(self, source_id, unit_id, name=""):
        plan = ServerPlan(stations=self.stations).copy_station(source_id, unit_id, name)
        result = replace(self, stations=plan.stations)
        result.validate()
        return result
