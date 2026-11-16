from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.application.contracts import Command, ErrorKind, JournalEntry, Point, ReadUpdate, TableReadUpdate, SubscriptionHealth
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


def test_subscription_keepalive_does_not_refresh_value_time_and_silence_expires():
    store, point, _table, tick = setup()
    update = event(point)
    update = replace(update, update=replace(update.update, entry=replace(update.update.entry, function="DataChangeNotification")))
    store.update(update)
    initial = store.samples["t", "p"]
    publish = initial.sampled_at
    store.update_health(SubscriptionHealth("t", 1, True, publish, publish, 5000, "正常"))
    tick[0] = 2
    store.update_health(SubscriptionHealth("t", 1, True, publish + timedelta(seconds=2), publish, 5000, "保活正常"))
    tick[0] = 4
    assert not store.expire()  # The unchanged value is still monitored by a live subscription.
    assert store.samples["t", "p"] == initial  # Keepalive is not a new sample or a value change.
    tick[0] = 7
    assert store.expire() == {("t", "p")}
    assert store.samples["t", "p"].quality == Quality.STALE
    store.update_health(SubscriptionHealth("t", 1, True, publish + timedelta(seconds=7), publish, 5000, "保活恢复"))
    assert store.samples["t", "p"].quality == Quality.STALE  # Old data is not relabelled as a fresh value.
    store.set_generation(2)
    store.update_health(SubscriptionHealth("t", 1, True, publish, publish, 5000, "旧会话"))
    assert not store.subscription_health


def test_last_value_change_is_independent_of_notification_time_and_stop_removes_health():
    store, point, _table, tick = setup()
    store.update(event(point))
    first = store.samples["t", "p"]
    store.update(event(point, request_id=2))
    second = store.samples["t", "p"]
    assert second.changed_at == first.changed_at and second.sampled_at > first.sampled_at
    store.update(event(point, value=236, request_id=3))
    assert store.samples["t", "p"].changed_at > first.changed_at
    store.update_health(SubscriptionHealth("t", 1, True, first.sampled_at, first.sampled_at, 10000))
    tick[0] = 4
    store.expire()
    assert store.samples["t", "p"].quality == Quality.STALE  # Keepalive cannot extend a polled value's lifetime.
    store.stop_subscription("t")
    assert not store.subscription_health
