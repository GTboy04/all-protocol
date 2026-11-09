import pytest

from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.values import DataType
from all_protocol.application.simulation import (
    Assignment, RuleKind, Simulation, SimulationCommand, SimulationRule, SimulationState, compile_rule,
)


def point(data_type, initial=0, rule=None, offset=0):
    area = Area.COIL if data_type == DataType.BOOL else Area.HR
    plan = ServerPlan(stations=(Station(1, points=(ServerPoint(area, offset, initial, "测试点", data_type,
                                                              simulation=rule),)),))
    return next(plan.simulation_points())


@pytest.mark.parametrize(("rule", "data_type", "initial", "times", "expected"), (
    (SimulationRule(RuleKind.FIXED, start_ms=30, value=7), DataType.INT16, 2, (0, 29, 30, 99), (None, None, 7, 7)),
    (SimulationRule(RuleKind.HEARTBEAT, period_ms=200), DataType.BOOL, True, (0, 199, 200, 400), (1, 1, 0, 1)),
    (SimulationRule(RuleKind.INCREMENT, period_ms=100, low=1, high=7, step=3), DataType.UINT16, 3,
     (0, 100, 200, 300, 400, 500), (3, 6, 1, 4, 7, 1)),
    (SimulationRule(RuleKind.RAMP, start_ms=200, period_ms=1000, high=2**64 - 1), DataType.UINT64, 2**64 - 5,
     (100, 200, 700, 1200, 1700), (None, 2**64 - 5, 2**64 - 3, 2**64 - 1, 2**64 - 1)),
    (SimulationRule(RuleKind.SINE, period_ms=100, amplitude=3), DataType.FLOAT32, 2,
     (0, 25, 50, 75, 100), (2, 5, 2, -1, 2)),
), ids=("fixed", "heartbeat", "increment", "ramp-uint64", "sine"))
def test_waveform_samples(rule, data_type, initial, times, expected):
    sample = compile_rule(point(data_type, initial), rule)
    for time, value in zip(times, expected, strict=True):
        assert sample(time) == (pytest.approx(value) if data_type.floating else value)


@pytest.mark.parametrize("rule", (
    SimulationRule(RuleKind.FIXED, value=-1),
    SimulationRule(RuleKind.HEARTBEAT),
    SimulationRule(RuleKind.RAMP, period_ms=0, high=3),
    SimulationRule(RuleKind.INCREMENT, low=4, high=10),
    SimulationRule(RuleKind.SINE, amplitude=10),
    SimulationRule(RuleKind.TIMELINE, actions=(Assignment(0, 3), Assignment(0, 5))),
    SimulationRule(RuleKind.TIMELINE, actions=(Assignment(100, 65536),)),
))
def test_invalid_rules_are_rejected_with_point_context(rule):
    with pytest.raises(ValueError, match="测试点"):
        compile_rule(point(DataType.UINT16, initial=2), rule)


def test_timeline_pause_takeover_release_and_reset_with_virtual_clock():
    timeline = SimulationRule(RuleKind.TIMELINE, actions=(
        Assignment(0, 1), Assignment(20, 2), Assignment(21, 3), Assignment(30, 4), Assignment(60, 5),
    ))
    target = point(DataType.UINT16, rule=timeline)
    writes = []
    now = 100.0
    scene = Simulation((target,), lambda: now, lambda key, value: writes.append((key, value)))

    def tick(time):
        nonlocal now
        now = time
        scene.tick()

    scene.command(SimulationCommand.START)
    tick(100.025)
    assert writes == [(target.target, value) for value in (1, 2, 3)]
    scene.command(SimulationCommand.PAUSE)
    tick(200.0)
    assert scene.snapshot().state == SimulationState.PAUSED and writes[-1][1] == 3
    scene.command(SimulationCommand.START)
    tick(200.005)
    assert writes[-1][1] == 4
    assert scene.take_over(target.span)
    tick(200.1)
    assert writes[-1][1] == 4
    scene.command(SimulationCommand.RELEASE, target.target)
    tick(200.1)
    assert writes[-1][1] == 5 and not scene.snapshot().held
    scene.take_over(target.span)
    scene.command(SimulationCommand.RESET)
    tick(201.0)
    assert writes[-1][1] == 0 and not scene.snapshot().held
    scene.command(SimulationCommand.START)
    tick(201.0)
    assert writes[-1][1] == 1


def test_runtime_failure_disables_only_that_rule_until_reset():
    rule = SimulationRule(RuleKind.FIXED, value=1)
    first, second = point(DataType.UINT16, rule=rule), point(DataType.UINT16, rule=rule, offset=1)
    writes = []

    def write(target, value):
        writes.append((target, value))
        if target == first.target and value == 1:
            raise ValueError("模拟写入越界")

    scene = Simulation((first, second), lambda: 1, write)
    scene.command(SimulationCommand.START)
    assert scene.tick()
    assert scene.snapshot().faults == ((first.target, f"{first.label} · 模拟写入越界"),)
    assert not scene.tick()
    assert writes == [(first.target, 1), (second.target, 1)]
    scene.command(SimulationCommand.RESET)
    assert not scene.snapshot().faults
    assert writes[-2:] == [(first.target, 0), (second.target, 0)]
