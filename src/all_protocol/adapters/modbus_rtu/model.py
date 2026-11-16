from dataclasses import dataclass
import re

from ..modbus_tcp.model import ModbusTarget
from ...application.contracts import ClientError, ErrorKind


def normalize_port(port: str) -> str:
    if not isinstance(port, str):
        raise ClientError(ErrorKind.INPUT, "串口名称必须为文本")
    name = port.strip()
    if name.startswith("\\\\.\\"):
        name = name[4:]
    if not re.fullmatch(r"COM[1-9][0-9]*", name, re.IGNORECASE):
        raise ClientError(ErrorKind.INPUT, "请输入 Windows COM 串口名称，例如 COM3")
    return f"COM{int(name[3:])}"


@dataclass(frozen=True, slots=True)
class ModbusRtuConfig:
    serial_port: str = "COM1"
    baudrate: int = 9600
    parity: str = "E"
    stopbits: int = 1
    timeout_ms: int = 3000

    @property
    def endpoint(self):
        return normalize_port(self.serial_port)

    def validate(self):
        normalize_port(self.serial_port)
        if (type(self.baudrate) is not int or not 300 <= self.baudrate <= 4000000
                or self.parity not in ("N", "E", "O") or type(self.stopbits) is not int
                or self.stopbits not in (1, 2) or type(self.timeout_ms) is not int
                or not 100 <= self.timeout_ms <= 30000):
            raise ClientError(ErrorKind.INPUT, "检查波特率、校验位、停止位和超时设置")

    @property
    def silent_interval(self):
        return 0.00175 if self.baudrate > 19200 else 3.5 * (9 + (self.parity != "N") + self.stopbits) / self.baudrate


@dataclass(frozen=True, slots=True)
class ModbusRtuTarget(ModbusTarget):
    def validate(self, values=None):
        if type(self.unit_id) is not int or not 1 <= self.unit_id <= 247:
            raise ClientError(ErrorKind.INPUT, "RTU 站号必须在 1–247 之间，不支持广播")
        return ModbusTarget.validate(self, values)
