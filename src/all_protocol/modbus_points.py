"""Portable Modbus server point definitions for a single station (all areas)."""

import csv
import io
import json
from dataclasses import replace

from .adapters.modbus_tcp.server_model import ServerPlan, Station
from .point_tables import IMPORT_LIMIT_BYTES
from .storage import decode_modbus_plan, encode_modbus_plan


FIELDS = ("area", "offset", "name", "data_type", "order", "initial", "simulation")


def export_station_points(station: Station, *, csv_format=False) -> str:
    plan = ServerPlan(stations=(station,))
    plan.validate()
    points = encode_modbus_plan(plan)["stations"][0]["points"]
    # Resolve legacy implicit types so CSV can be edited without protocol defaults.
    for record, point in zip(points, station.points, strict=True):
        record["data_type"] = point.value_type.value
    if not csv_format:
        return json.dumps({"schema_version": 1, "protocol": "modbus-tcp", "kind": "server-points",
                           "points": points}, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader()
    for record in points:
        record["initial"] = json.dumps(record["initial"], allow_nan=False)
        record["simulation"] = json.dumps(record["simulation"], ensure_ascii=False) if "simulation" in record else ""
        writer.writerow(record)
    return stream.getvalue()


def import_station_points(text: str, *, csv_format=False) -> tuple:
    if len(text.encode("utf-8")) > IMPORT_LIMIT_BYTES:
        raise ValueError("点位文件超过 10 MiB")
    text = text.removeprefix("\ufeff")
    try:
        if csv_format:
            reader = csv.DictReader(io.StringIO(text, newline=""))
            if reader.fieldnames != list(FIELDS):
                raise ValueError("CSV 表头必须为：" + ",".join(FIELDS))
            points = []
            for line, row in enumerate(reader, 2):
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("列数不匹配")
                    points.append({**row, "offset": int(row["offset"]), "initial": json.loads(row["initial"]),
                                   "simulation": json.loads(row["simulation"]) if row["simulation"].strip() else None})
                except (ValueError, TypeError) as error:
                    raise ValueError(f"CSV 第 {line} 行：{error}") from error
        else:
            record = json.loads(text)
            if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
                    or record["schema_version"] != 1 or record.get("protocol") != "modbus-tcp"
                    or record.get("kind") != "server-points"):
                raise ValueError("点位文件版本或协议无法识别")
            points = record["points"]
        plan = decode_modbus_plan({"schema_version": 3, "protocol": "modbus-tcp", "host": "127.0.0.1",
                                   "port": 1502, "stations": [{"unit_id": 1, "name": "", "points": points}]})
        return plan.stations[0].points
    except (KeyError, TypeError, AttributeError, RecursionError, OverflowError, csv.Error) as error:
        raise ValueError(f"点位文件格式无效：{error}") from error


def append_station_points(station: Station, points: tuple) -> Station:
    """Validate the whole result before changing anything; overlaps are errors."""
    updated = replace(station, points=station.points + points)
    ServerPlan(stations=(updated,)).validate()
    return updated
