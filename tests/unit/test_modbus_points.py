import json
from dataclasses import replace

import pytest

from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.values import DataType, RegisterOrder
from all_protocol.application.simulation import Assignment, RuleKind, SimulationRule
from all_protocol.modbus_points import append_station_points, export_station_points, import_station_points


def station():
    return Station(7, "源站", (
        ServerPoint(Area.HR, 0, 2**64 - 1, "计数,\"累计\"\n值", DataType.UINT64, RegisterOrder.LITTLE),
        ServerPoint(Area.IR, 65534, -12.5, "温度", DataType.FLOAT32, RegisterOrder.SWAP_WORDS),
        ServerPoint(Area.COIL, 65535, True, "开关", DataType.BOOL,
                    simulation=SimulationRule(RuleKind.HEARTBEAT)),
        ServerPoint(Area.DI, 0, 0, "反馈", DataType.BOOL,
                    simulation=SimulationRule(RuleKind.TIMELINE, actions=(Assignment(0, 1), Assignment(20, 0)))),
    ))


@pytest.mark.parametrize("csv_format", (False, True))
def test_round_trip_all_areas_types_orders_and_rules(csv_format):
    source = station()
    text = export_station_points(source, csv_format=csv_format)
    assert import_station_points("\ufeff" + text, csv_format=csv_format) == source.points
    target = append_station_points(Station(255, "目标"), import_station_points(text, csv_format=csv_format))
    assert target == replace(source, unit_id=255, name="目标")


def test_copy_station_checks_ids_and_preserves_source():
    source = station()
    plan = ServerPlan(stations=(source,))
    copied = plan.copy_station(7, 255, "副本")
    assert plan.stations == (source,)
    assert copied.stations == (source, replace(source, unit_id=255, name="副本"))
    for unit_id in (0, 256, True, 7):
        with pytest.raises(ValueError):
            plan.copy_station(7, unit_id)
    with pytest.raises(ValueError, match="不存在"):
        plan.copy_station(8, 9)


def test_append_rejects_partial_overlap_atomically():
    source = station()
    with pytest.raises(ValueError, match="已存在"):
        append_station_points(source, (ServerPoint(Area.HR, 20, 1), ServerPoint(Area.HR, 3, 1)))
    assert len(source.points) == 4


@pytest.mark.parametrize("mutate", (
    lambda r: r.update(protocol="s7"),
    lambda r: r.update(schema_version=True),
    lambda r: r.update(points={}),
    lambda r: r["points"][0].update(offset=65535),
    lambda r: r["points"][0].update(initial=-1),
    lambda r: r["points"][0].update(area="bad"),
    lambda r: r["points"][0].update(data_type="bad"),
    lambda r: r["points"][0].update(order="bad"),
    lambda r: r["points"][0].update(simulation={"kind": "sine", "value": 0, "amplitude": 1}),
    lambda r: r["points"].append(r["points"][0]),
    lambda r: r["points"].append(None),
))
def test_bad_json_is_rejected(mutate):
    record = json.loads(export_station_points(station()))
    mutate(record)
    with pytest.raises(ValueError):
        import_station_points(json.dumps(record))


@pytest.mark.parametrize("text", (
    "area,offset\nHR,0\n",
    "area,offset,name,data_type,order,initial,simulation\nHR,bad,x,UInt16,big,0,\n",
    "area,offset,name,data_type,order,initial,simulation\nHR,0\n",
    "area,offset,name,data_type,order,initial,simulation\nHR,0,x,UInt16,big,0,,extra\n",
))
def test_bad_csv_is_rejected(text):
    with pytest.raises(ValueError):
        import_station_points(text, csv_format=True)


def test_import_limits_size():
    with pytest.raises(ValueError, match="10 MiB"):
        import_station_points(" " * (10 * 1024 * 1024 + 1))
