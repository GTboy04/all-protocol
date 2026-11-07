from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.application.contracts import Command, ErrorKind, JournalEntry, Point, ReadUpdate, TableReadUpdate
from all_protocol.application.sampling import Quality, SampleStore
from all_protocol.point_tables import PointDefinition, PointTable


def setup():
    tick = [0.0]
    store = SampleStore(clock=lambda: tick[0])
    point = PointDefinition("p", "温度", ModbusTarget(1, Area.HR, 0, 1))
    table = PointTable("t", "d", "运行", (point,))
    store.sync((table,))
    store.set_generation(1)
    return store, point, table, tick


def event(point, value=235, *, error=None, request_id=1, generation=1):
    entry = JournalEntry(request_id, datetime(2026, 11, 7) + timedelta(seconds=request_id), "读取", "target", "read",
                         10, "成功" if error is None else error.value, error, "设备错误" if error else "")
    points = None if error else (Point("0", "value", value),)
    return TableReadUpdate("t", point.id, ReadUpdate(Command(point.target), entry, points, generation))


@pytest.mark.parametrize("error,quality", ((ErrorKind.TIMEOUT, Quality.TIMEOUT), (ErrorKind.DISCONNECTED, Quality.DISCONNECTED),
                                        (ErrorKind.PROTOCOL, Quality.PROTOCOL), (ErrorKind.RESPONSE, Quality.PROTOCOL)))
def test_failed_attempt_preserves_value_success_time_and_request_evidence(error, quality):
    store, point, _table, tick = setup()
    store.update(event(point))
    first = store.samples["t", "p"]
    tick[0] = 1
    store.update(event(point, error=error, request_id=2))
    sample = store.samples["t", "p"]
    assert sample.raw_value == 235 and sample.last_success_at == first.last_success_at
    assert sample.sampled_at > sample.last_success_at
    assert sample.quality == quality and sample.request_id == 2 and sample.detail == "设备错误"
    tick[0] = 100
    store.expire()
    assert store.samples["t", "p"].quality == quality


def test_expiration_uses_monotonic_time_without_erasing_value():
    store, point, _table, tick = setup()
    store.update(event(point))
    tick[0] = 2.999
    assert not store.expire()
    tick[0] = 3
    assert store.expire() == {("t", "p")}
    assert store.samples["t", "p"].quality == Quality.STALE
    assert store.samples["t", "p"].raw_value == 235


def test_old_generation_and_changed_address_cannot_overwrite_new_samples():
    store, point, table, _tick = setup()
    store.update(event(point))
    store.set_generation(2)
    store.set_generation(1)
    assert store.generation == 2
    assert store.samples["t", "p"].quality == Quality.DISCONNECTED
    assert not store.update(event(point, value=999))
    store.update(event(point, value=42, generation=2))
    assert store.samples["t", "p"].raw_value == 42
    changed = replace(point, target=replace(point.target, offset=4))
    store.sync((replace(table, points=(changed,)),))
    assert store.samples["t", "p"].quality == Quality.NEVER
    assert not store.update(event(point, value=999, generation=2))
    store.sync(())
    assert not store.samples


def test_non_finite_response_is_not_a_good_sample():
    store, point, _table, _tick = setup()
    store.update(event(point))
    store.update(event(point, value=float("nan"), request_id=2))
    assert store.samples["t", "p"].raw_value == 235
    assert store.samples["t", "p"].quality == Quality.PROTOCOL
