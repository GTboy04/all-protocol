from dataclasses import dataclass

import pytest

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.adapters.modbus_tcp.values import DataType, RegisterOrder
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7Type, S7Target
from all_protocol.application.contracts import Command
from all_protocol.application.scheduling import RequestScheduler, Work, table_work


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@dataclass(frozen=True)
class Target:
    label: str


def work(label):
    return Work(Command(Target(label)))


def finish(scheduler, *, successful=True):
    selected = scheduler.take()
    assert selected is not None
    label = selected.work.command.target.label
    scheduler.complete(selected, successful)
    return label


def test_manual_priority_is_bounded_and_due_groups_rotate():
    clock = Clock()
    scheduler = RequestScheduler(clock=clock)
    scheduler.start_group("A", (work("a0"), work("a1")), 1000)
    scheduler.start_group("B", (work("b0"), work("b1")), 1000)
    for index in range(6):
        scheduler.submit((work(f"m{index}"),))
    order = [finish(scheduler) for _ in range(10)]
    assert order == ["m0", "m1", "m2", "a0", "m3", "m4", "m5", "b0", "a1", "b1"]
    assert scheduler.take() is None
    assert scheduler.delay() == 1.0


def test_table_continuations_yield_and_capacity_counts_active_task():
    scheduler = RequestScheduler(capacity=2)
    first = scheduler.submit((work("a0"), work("a1")))
    scheduler.submit((work("b0"),))
    selected = scheduler.take()
    with pytest.raises(ValueError, match="未发送"):
        scheduler.submit((work("overflow"),))
    assert scheduler.take() is None
    scheduler.complete(selected, True)
    assert finish(scheduler) == "b0"
    assert finish(scheduler) == "a1"
    assert (first.id, True) in scheduler.finished
    assert not scheduler.operations


def test_slow_cycle_skips_missed_deadlines_without_backlog():
    clock = Clock()
    scheduler = RequestScheduler(clock=clock)
    scheduler.start_group("g", (work("p"),), 1000)
    active = scheduler.take()
    clock.now = 10.5
    scheduler.complete(active, False)
    assert scheduler.take() is None
    assert scheduler.delay() == 0.5
    clock.now = 11.0
    assert finish(scheduler) == "p"
    assert scheduler.delay() == 1.0


def test_stop_does_not_cancel_active_request_or_start_rest_of_cycle():
    scheduler = RequestScheduler()
    group = scheduler.start_group("g", (work("p0"), work("p1")), 200)
    active = scheduler.take()
    scheduler.stop_group("g")
    assert scheduler.active is active and not scheduler.finished
    scheduler.complete(active, True)
    assert scheduler.finished == [(group.id, True)]
    assert scheduler.take() is None


def test_failed_group_keeps_running_and_quick_failure_stops_only_itself():
    clock = Clock()
    scheduler = RequestScheduler(clock=clock)
    scheduler.start_group("quick", (work("quick"),), 200, stop_on_error=True)
    scheduler.start_group("table", (work("table"),), 200)
    assert finish(scheduler, successful=False) == "quick"
    assert finish(scheduler, successful=False) == "table"
    assert set(scheduler.groups) == {"table"}
    clock.now = 1.0
    assert finish(scheduler) == "table"


@pytest.mark.parametrize("interval", (True, 199, 30001, 1.0))
def test_invalid_group_period(interval):
    with pytest.raises(ValueError):
        RequestScheduler().start_group("g", (work("p"),), interval)


def test_group_capacity_duplicate_and_clear():
    scheduler = RequestScheduler(group_capacity=1)
    scheduler.start_group("g", (work("p"),), 1000)
    with pytest.raises(ValueError):
        scheduler.start_group("g", (work("p"),), 1000)
    with pytest.raises(ValueError):
        scheduler.start_group("other", (work("p"),), 1000)
    scheduler.submit((work("m"),))
    operations, groups = scheduler.clear()
    assert len(operations) == len(groups) == 1
    assert scheduler.delay() is None and scheduler.take() is None


@pytest.mark.parametrize("protocol,total,maximum", (("modbus", 126, 125), ("s7", 101, 100)))
def test_read_merge_respects_wire_limit_and_members(protocol, total, maximum):
    commands = tuple((str(index), Command(ModbusTarget(1, Area.HR, index, 1) if protocol == "modbus"
                                          else S7Target(S7Area.DB, 1, index * 2, S7Type.WORD)))
                     for index in range(total))
    batches = table_work("t", commands)
    assert [batch.command.target.count for batch in batches] == [maximum, total - maximum]
    assert tuple(member for batch in batches for member in batch.members) == commands


def test_merge_checks_station_area_type_order_gap_and_s7_bits():
    targets = (ModbusTarget(1, Area.HR, 0, 1), ModbusTarget(2, Area.HR, 1, 1),
               ModbusTarget(2, Area.IR, 2, 1), ModbusTarget(2, Area.IR, 3, 1, DataType.FLOAT32),
               ModbusTarget(2, Area.IR, 5, 1, DataType.FLOAT32, RegisterOrder.LITTLE),
               ModbusTarget(2, Area.IR, 8, 1, DataType.FLOAT32, RegisterOrder.LITTLE))
    assert len(table_work("t", tuple((str(i), Command(target)) for i, target in enumerate(targets)))) == len(targets)
    bits = (S7Target(S7Area.DB, 1, 0, S7Type.BOOL, bit=0), S7Target(S7Area.DB, 1, 0, S7Type.BOOL, bit=1))
    assert len(table_work("t", tuple((str(i), Command(target)) for i, target in enumerate(bits)))) == 2
    assert S7Target(S7Area.DB, 1, 0, S7Type.WORD).merged_with(S7Target(S7Area.DB, 2, 2, S7Type.WORD)) is None


def test_float64_register_merge_counts_words_and_coils_count_bits():
    doubles = tuple((str(i), Command(ModbusTarget(1, Area.HR, i * 4, 1, DataType.FLOAT64))) for i in range(32))
    assert [item.command.target.count for item in table_work("t", doubles)] == [31, 1]
    coils = tuple((str(i), Command(ModbusTarget(1, Area.COIL, i, 1))) for i in range(2001))
    assert [item.command.target.count for item in table_work("t", coils)] == [2000, 1]


def test_read_planning_never_drops_or_converts_a_write():
    with pytest.raises(ValueError, match="写请求"):
        table_work("t", (("p", Command(ModbusTarget(1, Area.HR, 0, 1), (42,))),))
