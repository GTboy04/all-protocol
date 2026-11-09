from dataclasses import dataclass
from ipaddress import ip_address

from ...application.contracts import ClientError, Value
from ...application.simulation import SimulationPoint, SimulationRule, WriteSpan, compile_rule
from .model import Area, DataType, S7Target


@dataclass(frozen=True, slots=True)
class S7ServerPoint:
    target: S7Target
    initial: Value = 0
    name: str = ""
    simulation: SimulationRule | None = None

    def encode(self, value: Value) -> bytes:
        try:
            return self.target.pack_values((value,))
        except ClientError as error:
            raise ValueError(str(error)) from error


@dataclass(frozen=True, slots=True)
class S7ServerPlan:
    host: str = "127.0.0.1"
    port: int = 1102
    points: tuple[S7ServerPoint, ...] = (S7ServerPoint(S7Target(Area.DB, 1, 0, DataType.WORD)),)

    def validate(self, *, starting: bool = False):
        try:
            if not isinstance(self.host, str):
                raise ValueError
            ip_address(self.host)
        except ValueError:
            raise ValueError("请输入有效的监听 IP 地址") from None
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("端口必须在 1–65535 之间")
        if starting and not self.points:
            raise ValueError("启动前请至少配置一个 S7 点位")
        occupied = {}
        for point in self.points:
            if not isinstance(point.name, str):
                raise ValueError("点位名称必须是文本")
            point.encode(point.initial)
            target = point.target
            mask = 1 << target.bit if target.data_type == DataType.BOOL else 0xFF
            for offset in range(target.offset, target.offset + target.byte_count):
                key = target.area, target.db, offset
                previous = occupied.get(key, 0)
                if previous & mask:
                    raise ValueError(f"{target.address()} 与已有点位重叠")
                occupied[key] = previous | mask
        for point in self.simulation_points(configured_only=True):
            compile_rule(point, point.rule)

    def simulation_points(self, *, configured_only=False):
        for point in self.points:
            if configured_only and point.simulation is None:
                continue
            target = point.target
            boolean = target.data_type == DataType.BOOL
            mask = 1 << target.bit if boolean else 0xFF
            span = WriteSpan((target.area, target.db), target.offset, target.offset + target.byte_count, mask)
            address = target.address()
            label = f"{point.name} · {address}" if point.name else address
            yield SimulationPoint(target, label, point.initial, point.encode, span, target.data_type == DataType.REAL,
                                  boolean, point.simulation)


@dataclass(frozen=True, slots=True)
class S7ValuesChanged:
    area: Area
    db: int
    offset: int
    data: bytes
    mask: int = 0xFF


class S7Memory:
    def __init__(self, points: tuple[S7ServerPoint, ...]):
        self._bytes: dict[tuple[Area, int, int], int] = {}
        for point in points:
            self.set_point(point, point.initial)

    def read(self, area: Area, db: int, offset: int, size: int) -> bytes:
        return bytes(self._bytes[area, db, index] for index in range(offset, offset + size))

    def apply(self, change: S7ValuesChanged):
        for offset, value in enumerate(change.data, change.offset):
            self._bytes[change.area, change.db, offset] = value

    def set_point(self, point: S7ServerPoint, value: Value) -> S7ValuesChanged:
        target = point.target
        data = point.encode(value)
        mask = 0xFF
        if target.data_type == DataType.BOOL:
            key = target.area, target.db, target.offset
            previous = self._bytes.get(key, 0)
            mask = 1 << target.bit
            data = bytes(((previous & ~mask) | (data[0] << target.bit),))
        change = S7ValuesChanged(target.area, target.db, target.offset, data, mask)
        self.apply(change)
        return change

    def current(self, point: S7ServerPoint) -> Value:
        target = point.target
        return target.decode(self.read(target.area, target.db, target.offset, target.byte_count))[0].value
