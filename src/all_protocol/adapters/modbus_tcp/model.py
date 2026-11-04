from dataclasses import dataclass
from enum import StrEnum

from ...application.contracts import ClientError, ErrorKind, Target, Value


class Area(StrEnum):
    HR = "HR"
    IR = "IR"
    COIL = "Coil"
    DI = "DI"


@dataclass(frozen=True, slots=True)
class AreaRule:
    label: str
    read_code: int
    read_limit: int
    value_limit: int
    write_codes: tuple[int, int] | None = None
    write_limit: int = 0


RULES = {
    Area.HR: AreaRule("保持寄存器", 0x03, 125, 65535, (0x06, 0x10), 123),
    Area.IR: AreaRule("输入寄存器", 0x04, 125, 65535),
    Area.COIL: AreaRule("线圈", 0x01, 2000, 1, (0x05, 0x0F), 1968),
    Area.DI: AreaRule("离散输入", 0x02, 2000, 1),
}


@dataclass(frozen=True, slots=True)
class ModbusTcpConfig:
    host: str
    port: int = 502
    timeout_ms: int = 3000

    @property
    def endpoint(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    def validate(self):
        if not self.host.strip() or any(char.isspace() for char in self.host):
            raise ClientError(ErrorKind.INPUT, "请输入 IP 地址或主机名")
        if not 1 <= self.port <= 65535 or not 100 <= self.timeout_ms <= 30000:
            raise ClientError(ErrorKind.INPUT, "检查端口和超时设置")


@dataclass(frozen=True, slots=True)
class ModbusTarget:
    unit_id: int
    area: Area
    offset: int
    count: int

    @property
    def label(self) -> str:
        address = str(self.offset) if self.count == 1 else f"{self.offset}–{self.offset + self.count - 1}"
        return f"站 {self.unit_id} · {self.area} · {address}"

    def validate(self, values: tuple[Value, ...] | None = None) -> int:
        rule = RULES[self.area]
        if not 1 <= self.unit_id <= 255:
            raise ClientError(ErrorKind.INPUT, "Unit ID 必须在 1–255 之间")
        if not 0 <= self.offset <= 65535 or self.offset + self.count > 65536:
            raise ClientError(ErrorKind.INPUT, "地址范围超出 0–65535")
        if values is None:
            if not 1 <= self.count <= rule.read_limit:
                raise ClientError(ErrorKind.INPUT, f"读取数量必须在 1–{rule.read_limit} 之间")
            return rule.read_code
        if rule.write_codes is None:
            raise ClientError(ErrorKind.INPUT, "当前数据区只读")
        if not 1 <= self.count <= rule.write_limit or len(values) != self.count:
            raise ClientError(ErrorKind.INPUT, f"写入数量需与值的个数一致，上限为 {rule.write_limit}")
        if any(not isinstance(value, int) or not 0 <= value <= rule.value_limit for value in values):
            raise ClientError(ErrorKind.INPUT, f"写入值必须是 0–{rule.value_limit} 的整数")
        return rule.write_codes[0 if self.count == 1 else 1]


def modbus_target(target: Target) -> ModbusTarget:
    if not isinstance(target, ModbusTarget):
        raise ClientError(ErrorKind.INPUT, "点位格式与 Modbus TCP 不匹配")
    return target
