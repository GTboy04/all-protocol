from dataclasses import dataclass, replace

from .model import RULES, Area


def validate_value(area: Area, value: int):
    if type(value) not in (int, bool) or not 0 <= value <= RULES[area].value_limit:
        raise ValueError(f"{area} 的值必须在 0–{RULES[area].value_limit} 之间")


@dataclass(frozen=True, slots=True)
class ServerPoint:
    area: Area
    offset: int
    initial: int
    name: str = ""

    def validate(self):
        if type(self.offset) is not int or not 0 <= self.offset <= 65535:
            raise ValueError(f"偏移 {self.offset} 超出 0–65535")
        if not isinstance(self.name, str):
            raise ValueError("点位名称必须是文本")
        validate_value(self.area, self.initial)


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
            key = point.area, point.offset
            if key in seen:
                raise ValueError(f"站 {self.unit_id} 的 {point.area} / {point.offset} 已存在")
            seen.add(key)

    def add_points(self, area: Area, offset: int, count: int, initial: int, name: str = ""):
        if type(count) is not int or count < 1:
            raise ValueError("数量必须大于 0")
        validate_value(area, initial)
        occupied = {point.offset for point in self.points if point.area == area}
        for address in range(offset, offset + count):
            if not 0 <= address <= 65535:
                raise ValueError(f"偏移 {address} 超出 0–65535")
            if address in occupied:
                raise ValueError(f"站 {self.unit_id} 的 {area} / {address} 已存在")
        points = tuple(ServerPoint(area, address, initial, name) for address in range(offset, offset + count))
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
