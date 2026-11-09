from dataclasses import dataclass, replace

from ...application.contracts import Value
from ...application.simulation import SimulationPoint, SimulationRule, WriteSpan, compile_rule
from .model import Area, data_types
from .values import DataType, RegisterOrder


@dataclass(frozen=True, slots=True)
class ModbusServerTarget:
    unit_id: int
    area: Area
    offset: int


@dataclass(frozen=True, slots=True)
class ServerPoint:
    area: Area
    offset: int
    initial: Value
    name: str = ""
    data_type: DataType | None = None
    order: RegisterOrder = RegisterOrder.BIG
    simulation: SimulationRule | None = None

    @property
    def value_type(self) -> DataType:
        return self.data_type if self.data_type is not None else data_types(self.area)[0]

    @property
    def addresses(self) -> range:
        return range(self.offset, self.offset + self.value_type.words)

    def encode(self, value: Value) -> tuple[int, ...]:
        return self.value_type.encode(value, self.order)

    def decode(self, words: tuple[int, ...]) -> Value:
        return self.value_type.decode(words, self.order)

    def validate(self):
        if type(self.offset) is not int or not 0 <= self.offset <= 65535:
            raise ValueError(f"偏移 {self.offset} 超出 0–65535")
        if not isinstance(self.name, str):
            raise ValueError("点位名称必须是文本")
        if not isinstance(self.value_type, DataType) or self.value_type not in data_types(self.area):
            raise ValueError(f"{self.area} 数据区不支持此类型")
        if not isinstance(self.order, RegisterOrder):
            raise ValueError("无效的寄存器顺序")
        if self.addresses[-1] > 65535:
            raise ValueError(f"偏移 {self.addresses[-1]} 超出 0–65535")
        self.encode(self.initial)


@dataclass(frozen=True, slots=True)
class Station:
    unit_id: int
    name: str = ""
    points: tuple[ServerPoint, ...] = ()

    def validate(self):
        if type(self.unit_id) is not int or not 1 <= self.unit_id <= 255:
            raise ValueError("Unit ID 必须在 1–255 之间")
        if not isinstance(self.name, str):
            raise ValueError("从站名称必须是文本")
        seen = set()
        for point in self.points:
            point.validate()
            for address in point.addresses:
                key = point.area, address
                if key in seen:
                    raise ValueError(f"站 {self.unit_id} 的 {point.area} / {address} 已存在")
                seen.add(key)

    def add_points(self, area: Area, offset: int, count: int, initial: Value, name: str = "",
                   data_type: DataType | None = None, order: RegisterOrder = RegisterOrder.BIG):
        if type(count) is not int or count < 1:
            raise ValueError("数量必须大于 0")
        first = ServerPoint(area, offset, initial, name, data_type, order)
        first.validate()
        words = first.value_type.words
        end = offset + count * words
        if end > 65536:
            raise ValueError(f"偏移 {end - 1} 超出 0–65535")
        occupied = {address for point in self.points if point.area == area for address in point.addresses}
        for address in range(offset, end):
            if address in occupied:
                raise ValueError(f"站 {self.unit_id} 的 {area} / {address} 已存在")
        points = tuple(replace(first, offset=address) for address in range(offset, end, words))
        return replace(self, points=self.points + points)


@dataclass(frozen=True, slots=True)
class ServerPlan:
    host: str = "127.0.0.1"
    port: int = 1502
    stations: tuple[Station, ...] = ()

    def validate(self, *, starting: bool = False):
        if not isinstance(self.host, str) or not self.host or any(char.isspace() for char in self.host):
            raise ValueError("请输入有效的监听地址")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("端口必须在 1–65535 之间")
        seen = set()
        for station in self.stations:
            station.validate()
            if station.unit_id in seen:
                raise ValueError(f"Unit ID {station.unit_id} 已存在")
            seen.add(station.unit_id)
        if starting and not any(station.points for station in self.stations):
            raise ValueError("启动前请至少配置一个从站和一个点位")
        for point in self.simulation_points(configured_only=True):
            compile_rule(point, point.rule)

    def simulation_points(self, *, configured_only=False):
        for station in self.stations:
            for point in station.points:
                if configured_only and point.simulation is None:
                    continue
                target = ModbusServerTarget(station.unit_id, point.area, point.offset)
                address = f"Unit {station.unit_id} / {point.area} / {point.offset}"
                span = WriteSpan((station.unit_id, point.area), point.offset, point.addresses.stop)
                label = f"{point.name} · {address}" if point.name else address
                yield SimulationPoint(target, label, point.initial, point.encode, span, point.value_type.floating,
                                      point.value_type == DataType.BOOL, point.simulation)
