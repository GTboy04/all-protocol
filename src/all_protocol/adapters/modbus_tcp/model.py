from dataclasses import dataclass, replace
from enum import StrEnum

from ...application.contracts import ClientError, ErrorKind, Target, Value
from .values import DataType, RegisterOrder


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

REGISTER_TYPES = tuple(data_type for data_type in DataType if data_type != DataType.BOOL)


def data_types(area: Area) -> tuple[DataType, ...]:
    return (DataType.BOOL,) if RULES[area].value_limit == 1 else REGISTER_TYPES


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
    data_type: DataType | None = None
    order: RegisterOrder = RegisterOrder.BIG

    def merged_with(self, other):
        if (not isinstance(other, ModbusTarget) or other.count != 1 or self.unit_id != other.unit_id
                or self.area != other.area or self.value_type != other.value_type or self.order != other.order
                or self.offset + self.register_count != other.offset):
            return None
        merged = replace(self, count=self.count + 1)
        try:
            merged.validate()
        except ClientError:
            return None
        return merged

    @property
    def value_type(self) -> DataType:
        return self.data_type if self.data_type is not None else data_types(self.area)[0]

    @property
    def register_count(self) -> int:
        return self.count * self.value_type.words

    @property
    def label(self) -> str:
        size = self.register_count
        address = str(self.offset) if size == 1 else f"{self.offset}–{self.offset + size - 1}"
        suffix = f" · {self.data_type}" if self.data_type is not None else ""
        return f"站 {self.unit_id} · {self.area} · {address}{suffix}"

    def encode_values(self, values: tuple[Value, ...]) -> tuple[int, ...]:
        try:
            return tuple(word for value in values for word in self.value_type.encode(value, self.order))
        except ValueError as error:
            raise ClientError(ErrorKind.INPUT, str(error)) from error

    def validate(self, values: tuple[Value, ...] | None = None) -> int:
        rule = RULES[self.area]
        if not 1 <= self.unit_id <= 255:
            raise ClientError(ErrorKind.INPUT, "Unit ID 必须在 1–255 之间")
        if not isinstance(self.value_type, DataType) or self.value_type not in data_types(self.area):
            raise ClientError(ErrorKind.INPUT, "该数据区不支持此类型")
        if not isinstance(self.order, RegisterOrder):
            raise ClientError(ErrorKind.INPUT, "无效的寄存器顺序")
        if not 0 <= self.offset <= 65535 or self.offset + self.register_count > 65536:
            raise ClientError(ErrorKind.INPUT, "地址范围超出 0–65535")
        if values is None:
            if not 1 <= self.register_count <= rule.read_limit:
                raise ClientError(ErrorKind.INPUT, f"读取数量必须在 1–{rule.read_limit // self.value_type.words} 之间")
            return rule.read_code
        if rule.write_codes is None:
            raise ClientError(ErrorKind.INPUT, "当前数据区只读")
        if not 1 <= self.register_count <= rule.write_limit or len(values) != self.count:
            limit = rule.write_limit // self.value_type.words
            raise ClientError(ErrorKind.INPUT, f"写入数量需与值的个数一致，上限为 {limit}")
        self.encode_values(values)
        return rule.write_codes[0 if self.register_count == 1 else 1]


def modbus_target(target: Target) -> ModbusTarget:
    if not isinstance(target, ModbusTarget):
        raise ClientError(ErrorKind.INPUT, "点位格式与 Modbus TCP 不匹配")
    return target
