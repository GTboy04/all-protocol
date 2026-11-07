"""Named point configuration and portable point-table exchange formats."""

import csv
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
import io
import json
import math
from uuid import uuid4

from .adapters.modbus_tcp.model import Area, ModbusTarget
from .adapters.modbus_tcp.values import DataType, RegisterOrder
from .adapters.s7.model import Area as S7Area, DataType as S7DataType, S7Target
from .application.contracts import ClientError, Value


type PointTarget = ModbusTarget | S7Target
CSV_COLUMNS = ("id", "name", "area", "address_offset", "data_type", "unit", "scale", "offset",
               "precision", "description", "unit_id", "db", "bit", "order")
IMPORT_LIMIT_BYTES = 10 * 1024 * 1024


def new_id() -> str:
    return str(uuid4())


def _text(value, label, *, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{label}必须是{'非空' if required else ''}文本")


def target_protocol(target: PointTarget) -> str:
    if isinstance(target, ModbusTarget):
        return "modbus-tcp"
    if isinstance(target, S7Target):
        return "s7"
    raise ValueError("不支持的点位目标")


def target_record(target: PointTarget) -> dict:
    if isinstance(target, ModbusTarget):
        return {"unit_id": target.unit_id, "area": target.area.value, "offset": target.offset,
                "count": target.count, "data_type": target.value_type.value, "order": target.order.value}
    if isinstance(target, S7Target):
        return {"area": target.area.name, "db": target.db, "offset": target.offset,
                "data_type": target.data_type.value, "count": target.count, "bit": target.bit}
    raise ValueError("不支持的点位目标")


def decode_target(protocol: str, data) -> PointTarget:
    if not isinstance(data, dict):
        raise ValueError("target 必须是对象")
    integers = ("unit_id", "offset", "count") if protocol == "modbus-tcp" else ("db", "offset", "count", "bit")
    if any(type(data.get(key)) is not int for key in integers):
        raise ValueError("目标地址、数量和位索引必须是整数")
    try:
        if protocol == "modbus-tcp":
            target = ModbusTarget(data["unit_id"], Area(data["area"]), data["offset"], data["count"],
                                  DataType(data["data_type"]), RegisterOrder(data["order"]))
        elif protocol == "s7":
            target = S7Target(S7Area[data["area"]], data["db"], data["offset"], S7DataType(data["data_type"]),
                              data["count"], data["bit"])
        else:
            raise ValueError(f"不支持的协议：{protocol}")
        target.validate()
        if target.count != 1:
            raise ValueError("每个命名点位只表示一个值，数量必须为 1")
        return target
    except (KeyError, TypeError, ClientError) as error:
        raise ValueError(f"无效点位目标：{error}") from error


@dataclass(frozen=True)
class PointDefinition:
    id: str
    name: str
    target: PointTarget
    unit: str = ""
    scale: float = 1.0
    offset: float = 0.0
    description: str = ""
    precision: int = 3

    def __post_init__(self):
        if isinstance(self.target, ModbusTarget) and self.target.data_type is None:
            object.__setattr__(self, "target", replace(self.target, data_type=self.target.value_type))

    def validate(self, protocol: str):
        for value, label in ((self.id, "点位 ID"), (self.name, "点位名称")):
            _text(value, label, required=True)
        _text(self.unit, "单位")
        _text(self.description, "说明")
        if target_protocol(self.target) != protocol:
            raise ValueError("点位协议与设备协议不一致")
        decode_target(protocol, target_record(self.target))
        for value in (self.scale, self.offset):
            if type(value) not in (int, float):
                raise ValueError("倍率和偏置必须是有限数值")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError("倍率和偏置必须是有限数值")
        if type(self.precision) is not int or not 0 <= self.precision <= 12:
            raise ValueError("显示精度必须为 0–12 的整数")

    def engineering_text(self, raw: Value) -> str:
        # Preserve UInt64 integer precision instead of passing through float.
        if type(raw) not in (int, float, bool):
            raise ValueError("工程值转换仅支持数值")
        number = Decimal(int(raw)) if isinstance(raw, bool) else Decimal(str(raw))
        if not number.is_finite():
            raise ValueError("原始值不是有限数值")
        with localcontext() as context:
            context.prec = 80
            result = number * Decimal(str(self.scale)) + Decimal(str(self.offset))
            return f"{result:.{self.precision}f}"


@dataclass(frozen=True)
class PointTable:
    id: str
    device_id: str
    name: str
    points: tuple[PointDefinition, ...] = ()
    interval_ms: int = 1000
    stale_after_ms: int = 3000

    def validate(self, protocol: str):
        if type(self.interval_ms) is not int or not 200 <= self.interval_ms <= 30000:
            raise ValueError("轮询周期必须为 200–30000 ms")
        if type(self.stale_after_ms) is not int or not 200 <= self.stale_after_ms <= 3600000:
            raise ValueError("过期时间必须为 200–3600000 ms")
        for value, label in ((self.id, "点表 ID"), (self.device_id, "设备 ID"), (self.name, "点表名称")):
            _text(value, label, required=True)
        ids = set()
        for index, point in enumerate(self.points):
            try:
                point.validate(protocol)
                if point.id in ids:
                    raise ValueError("点位 ID 重复")
                ids.add(point.id)
            except (ValueError, ClientError) as error:
                raise ValueError(f"points[{index}]：{error}") from error

    def duplicate(self, name: str, *, device_id: str | None = None) -> "PointTable":
        return PointTable(new_id(), device_id or self.device_id, name,
                          tuple(replace(point, id=new_id()) for point in self.points), self.interval_ms, self.stale_after_ms)


def point_record(point: PointDefinition) -> dict:
    return {"id": point.id, "name": point.name, "target": target_record(point.target), "unit": point.unit,
            "scale": point.scale, "offset": point.offset, "description": point.description, "precision": point.precision}


def table_record(table: PointTable) -> dict:
    return {"id": table.id, "device_id": table.device_id, "name": table.name,
            "points": [point_record(point) for point in table.points],
            "interval_ms": table.interval_ms, "stale_after_ms": table.stale_after_ms}


def decode_table(data, protocol: str) -> PointTable:
    try:
        if not isinstance(data, dict) or not isinstance(data["points"], list):
            raise ValueError("点表必须包含 points 列表")
        points = []
        for index, item in enumerate(data["points"]):
            try:
                if not isinstance(item, dict):
                    raise ValueError("点位必须是对象")
                points.append(PointDefinition(item["id"], item["name"], decode_target(protocol, item["target"]),
                                               item.get("unit", ""), item.get("scale", 1.0), item.get("offset", 0.0),
                                               item.get("description", ""), item.get("precision", 3)))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"points[{index}]：{error}") from error
        table = PointTable(data["id"], data["device_id"], data["name"], tuple(points),
                           data.get("interval_ms", 1000), data.get("stale_after_ms", 3000))
        table.validate(protocol)
        return table
    except (KeyError, TypeError) as error:
        raise ValueError(f"点表字段无效：{error}") from error


def export_table_json(table: PointTable, protocol: str) -> str:
    table.validate(protocol)
    record = {"format": "all-protocol-point-table", "schema_version": 2, "protocol": protocol,
              "table": table_record(table)}
    return json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def import_table_json(contents: str, protocol: str, device_id: str) -> PointTable:
    def reject(value):
        raise ValueError(f"不允许非有限数值：{value}")

    try:
        data = json.loads(contents, parse_constant=reject)
        if (not isinstance(data, dict) or data.get("format") != "all-protocol-point-table"
                or type(data.get("schema_version")) is not int or data["schema_version"] not in (1, 2)):
            raise ValueError("无法识别点表格式或版本")
        if data["protocol"] != protocol:
            raise ValueError("导入点表的协议与当前设备不同")
        if data["schema_version"] == 2 and not {"interval_ms", "stale_after_ms"}.issubset(data["table"]):
            raise ValueError("v2 点表缺少轮询周期或过期时间")
        return decode_table(data["table"], protocol).duplicate(data["table"]["name"], device_id=device_id)
    except (KeyError, TypeError, RecursionError) as error:
        raise ValueError(f"点表文件无效：{error}") from error


def export_points_csv(points: tuple[PointDefinition, ...]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for point in points:
        target = target_record(point.target)
        row = {"id": point.id, "name": point.name, "area": target["area"], "address_offset": target["offset"],
               "data_type": target["data_type"], "unit": point.unit, "scale": point.scale, "offset": point.offset,
               "precision": point.precision, "description": point.description}
        row.update({key: target[key] for key in ("unit_id", "db", "bit", "order") if key in target})
        writer.writerow(row)
    return output.getvalue()


def import_points_csv(contents: str, protocol: str, *, fresh_ids=False) -> tuple[PointDefinition, ...]:
    reader = csv.DictReader(io.StringIO(contents.lstrip("\ufeff"), newline=""), strict=True)
    points, ids = [], set()
    try:
        columns = reader.fieldnames
        required = {"name", "area", "address_offset", "data_type"}
        if (columns is None or len(columns) != len(set(columns)) or not required.issubset(columns)
                or set(columns) - set(CSV_COLUMNS)):
            raise ValueError("CSV 表头必须含 name,area,address_offset,data_type，且不能有重复或未知列")
        for row in reader:
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("列数与表头不一致")
                target = {"area": row["area"], "offset": int(row["address_offset"]), "count": 1,
                          "data_type": row["data_type"]}
                if protocol == "modbus-tcp":
                    target.update(unit_id=int(row.get("unit_id") or "1"), order=row.get("order") or "big")
                else:
                    target.update(db=int(row.get("db") or ("1" if row["area"] == "DB" else "0")),
                                  bit=int(row.get("bit") or "0"))
                point = PointDefinition(new_id() if fresh_ids else row.get("id") or new_id(), row["name"],
                                        decode_target(protocol, target), row.get("unit", ""),
                                        float(row.get("scale") or "1"), float(row.get("offset") or "0"),
                                        row.get("description", ""), int(row.get("precision") or "3"))
                point.validate(protocol)
                if point.id in ids:
                    raise ValueError("点位 ID 重复")
                ids.add(point.id)
                points.append(point)
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(f"CSV 第 {reader.line_num} 行：{error}") from error
    except csv.Error as error:
        raise ValueError(f"CSV 第 {reader.reader.line_num} 行：{error}") from error
    return tuple(points)
