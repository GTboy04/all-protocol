from dataclasses import replace

import pytest

from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.values import DataType
from all_protocol.application.scenario_model import (
    ConditionalRule, PointCommit, PointRef, ScenarioExtension, ScenarioValue, SequenceStep,
    check_extension, decode_extension, extension_digest, extension_record, validate_extensions,
)
from all_protocol.application.scenario_runtime import ScenarioRuntime
from all_protocol.scenario_worker import compile_source
from all_protocol.ui.scenario_extension_editor import EXAMPLE


def station(*, rule_fields=None, extension_fields=None, initial=0):
    config = Station(7, points=(ServerPoint(Area.HR, 0, initial), ServerPoint(Area.HR, 10, 0), ServerPoint(Area.COIL, 0, 0)))
    rules = (ConditionalRule("run", PointRef("HR", 0), 10, (
        SequenceStep(0, (ScenarioValue(PointRef("HR", 10), 1), ScenarioValue(PointRef("Coil", 0), 0))),
        SequenceStep(2000, (ScenarioValue(PointRef("HR", 10), 2),)),
        SequenceStep(5000, (ScenarioValue(PointRef("HR", 10), 3),)),
        SequenceStep(8000, (ScenarioValue(PointRef("HR", 10), 0), ScenarioValue(PointRef("Coil", 0), 1))),
    ), **(rule_fields or {})),)
    extension = check_extension(config, ScenarioExtension("extension", "联动", rules, **(extension_fields or {})))
    return replace(config, extensions=(extension,))


class Harness:
    def __init__(self, config=None):
        self.now = 0.0
        self.config = config or station()
        plan = ServerPlan(stations=(self.config,))
        self.points = tuple(plan.simulation_points())
        self.source, self.output, self.done = (p.target for p in self.points)
        self.values = {p.target: p.initial for p in self.points}
        self.offline = set()
        self.events, self.writes, self.restored = [], [], []
        self.runtime = ScenarioRuntime(tuple(plan.scenario_bindings()), self.points, lambda: self.now,
                                       self.values.__getitem__, self.write, lambda t: t.unit_id not in self.offline,
                                       self.events.append, self.restored.append)

    def write(self, values):
        self.values.update(values)
        self.writes.append((self.now, values))

    def change(self, value, origin="manual"):
        previous = self.values[self.source]
        self.values[self.source] = value
        self.runtime.committed(PointCommit(len(self.events) + 1, ((self.source, previous, value),)), origin)

    def tick(self, now):
        self.now = now
        self.runtime.tick()


def test_enter_edge_absolute_delays_busy_and_hold():
    h = Harness()
    h.runtime.start()
    h.change(10)
    h.tick(0)
    assert h.values[h.output] == 1
    h.change(0)
    h.change(10)
    h.tick(0.001)
    assert sum(e.action == "started" for e in h.events) == 1
    assert any(e.action == "ignored" for e in h.events)
    for time, expected in ((1.999, 1), (2, 2), (4.999, 2), (5, 3), (7.999, 3), (8, 0)):
        h.tick(time)
        assert h.values[h.output] == expected
    assert h.values[h.done] == 1
    assert h.output in h.runtime.arbiter.owners
    h.change(10)
    h.tick(8.1)
    assert sum(e.action == "started" for e in h.events) == 1
    h.change(0)
    h.change(10)
    h.tick(8.1)
    assert h.values[h.output] == 1
    assert sum(e.action == "started" for e in h.events) == 2


def test_sub_tick_pulse_is_retained_and_internal_writes_do_not_trigger():
    h = Harness()
    h.runtime.start()
    h.change(10, "extension")
    h.tick(0)
    assert not h.writes
    h.change(0)
    h.change(10, "base")
    h.change(0, "base")
    h.tick(0.001)
    assert h.values[h.output] == 1


def test_pause_keeps_remaining_time_and_rearms_without_pause_edges():
    h = Harness()
    h.runtime.start()
    h.change(10)
    h.tick(0)
    h.tick(1)
    h.runtime.pause()
    h.change(0)
    h.tick(101)
    assert h.values[h.output] == 1
    h.runtime.start()
    h.tick(101.999)
    assert h.values[h.output] == 1
    h.tick(102)
    assert h.values[h.output] == 2
    assert sum(e.action == "started" for e in h.events) == 1


@pytest.mark.parametrize("completion", ("hold", "release"))
def test_completion_and_explicit_release_restore_all_outputs(completion):
    h = Harness(station(rule_fields={"completion": completion}))
    h.runtime.start()
    h.change(10)
    for time in (0, 2, 5, 8):
        h.tick(time)
    if completion == "hold":
        assert h.runtime.arbiter.owners
        h.runtime.release(7, "extension")
    assert not h.runtime.arbiter.owners
    assert set(h.restored) == {h.output, h.done}


def test_manual_takeover_cancels_all_steps_and_blocks_retrigger_until_release():
    h = Harness()
    h.runtime.start()
    h.change(10)
    h.tick(0)
    h.runtime.manual(h.points[1].span)
    h.values[h.output] = 99
    h.tick(2)
    assert h.values[h.output] == 99
    assert not h.runtime.arbiter.owners
    h.change(0)
    h.change(10)
    h.tick(2)
    assert h.values[h.output] == 99
    assert any(e.action == "rejected" for e in h.events)
    h.runtime.arbiter.manual.discard(h.output)
    h.change(0)
    h.change(10)
    h.tick(2)
    assert h.values[h.output] == 1


def test_station_stop_reset_and_new_baseline_do_not_replay_old_actions():
    h = Harness()
    h.runtime.start()
    h.change(10)
    h.tick(0)
    h.offline.add(7)
    h.runtime.station_changed(7, False)
    h.tick(2)
    assert h.values[h.output] == 1
    h.runtime.reset()
    assert h.runtime.pending_reset == {h.output, h.done}
    h.offline.clear()
    h.runtime.station_changed(7, True)
    assert h.values[h.output] == 0
    h.runtime.start()
    h.tick(10)
    assert h.values[h.output] == 0
    assert len([e for e in h.events if e.action == "started"]) == 1


def test_late_step_and_failed_batch_cancel_without_further_actions():
    h = Harness()
    h.runtime.start()
    h.change(10)
    h.tick(0)
    h.tick(3.001)
    assert h.values[h.output] == 1
    assert any("1000 ms" in e.result for e in h.events)
    h.change(0)
    h.change(10)
    def fail(_):
        raise ValueError("失败")
    h.runtime.write = fail
    h.tick(3.001)
    assert not h.runtime.arbiter.owners
    assert any(e.action == "failed" for e in h.events)
    assert not next(iter(h.runtime.rules.values())).enabled


@pytest.mark.parametrize("fire_initial", (False, True))
def test_initial_satisfied_does_not_fire_on_resume(fire_initial):
    h = Harness(station(initial=10, rule_fields={"fire_initial": fire_initial}))
    h.runtime.start()
    h.tick(0)
    assert bool(h.writes) == fire_initial
    h.runtime.pause()
    h.runtime.start()
    h.tick(0)
    assert sum(e.action == "started" for e in h.events) == int(fire_initial)


def test_cancel_on_false_and_disabled_rule():
    h = Harness(station(rule_fields={"cancel_on_false": True}))
    h.runtime.start()
    h.change(10)
    h.tick(0)
    h.change(0)
    h.tick(2)
    assert h.values[h.output] == 1
    assert not h.runtime.arbiter.owners
    h.runtime.set_enabled(7, "extension", False)
    h.change(10)
    h.tick(2)
    assert sum(e.action == "started" for e in h.events) == 1


def test_sdk_compilation_in_worker_matches_example_and_errors_are_bounded():
    rules = compile_source(EXAMPLE)
    assert [s.at_ms for s in rules[0].steps] == [0, 2000, 5000, 8000]
    check_extension(station(), ScenarioExtension("python", "代码", rules, mode="python", source=EXAMPLE))
    with pytest.raises(ValueError, match="SyntaxError"):
        compile_source("def broken(:")
    with pytest.raises(ValueError, match="超时"):
        compile_source("while True: pass", timeout=.25)
    with pytest.raises(ValueError, match="build"):
        compile_source("x = 1")


def test_validation_prevents_stale_signature_missing_targets_and_overlapping_outputs():
    config = station()
    extension = config.extensions[0]
    config.validate()
    other = replace(extension, id="other")
    with pytest.raises(ValueError, match="冲突"):
        replace(config, extensions=(extension, other)).validate()
    with pytest.raises(ValueError, match="不存在"):
        replace(config, points=config.points[:1]).validate()
    with pytest.raises(ValueError, match="类型或字序"):
        replace(config, points=(replace(config.points[0], data_type=DataType.INT16), *config.points[1:])).validate()
    with pytest.raises(ValueError, match="重新检查"):
        replace(config, extensions=(replace(extension, source="modified"),)).validate()
    copied = ServerPlan(stations=(config,)).copy_station(7, 8).stations[1]
    assert copied.extensions[0].id != extension.id
    assert not copied.extensions[0].enabled
    assert not copied.extensions[0].checked_digest


def test_step_validation_values_time_and_floating_tolerance():
    config = station()
    extension = config.extensions[0]
    for step in (SequenceStep(1, extension.rules[0].steps[0].values),
                 SequenceStep(0, (ScenarioValue(PointRef("HR", 10), 65536),)),
                 SequenceStep(0, (ScenarioValue(PointRef("HR", 10), 1),) * 2)):
        with pytest.raises(ValueError):
            check_extension(config, replace(extension, rules=(replace(extension.rules[0], steps=(step,)),)))
    floating = replace(config, points=(replace(config.points[0], data_type=DataType.FLOAT32), *config.points[1:]), extensions=())
    with pytest.raises(ValueError, match="abs_tol"):
        check_extension(floating, extension)
    checked = check_extension(floating, replace(extension, rules=(replace(extension.rules[0], abs_tol=.001, rel_tol=.001),)))
    assert checked.checked_digest == extension_digest(checked)


def test_resource_limit_rejects_extra_runs_and_releases_cancelled_heap_entries():
    stations = []
    for unit in (1, 2, 3):
        config = Station(unit, points=tuple(ServerPoint(Area.HR, offset, 0) for offset in range(101)))
        rules = tuple(ConditionalRule(str(offset), PointRef("HR", 0), 1,
                                     (SequenceStep(1000, (ScenarioValue(PointRef("HR", offset), 1),)),))
                      for offset in range(1, 101))
        extension = check_extension(config, ScenarioExtension("many", "并发", rules))
        stations.append(replace(config, extensions=(extension,)))
    plan = ServerPlan(stations=tuple(stations))
    points = tuple(plan.simulation_points())
    values = {point.target: point.initial for point in points}
    events = []
    runtime = ScenarioRuntime(tuple(plan.scenario_bindings()), points, lambda: 0, values.__getitem__,
                              lambda batch: values.update(batch), lambda target: True, events.append, lambda _: None)
    runtime.start()
    runtime.committed(PointCommit(1, tuple((point.target, 0, 1) for point in points if point.target.offset == 0)), "manual")
    runtime.tick()
    assert sum(e.action == "started" for e in events) == 256
    assert sum(e.action == "rejected" for e in events) == 44
    runtime.set_enabled(1, "many", False)
    assert len(runtime.heap) == 156
    runtime.close()
    assert not runtime.heap and not runtime.arbiter.owners


def test_enabling_conflicting_disabled_extension_is_rejected_and_pending_cancelled():
    config = station()
    other = replace(config.extensions[0], id="disabled", enabled=False)
    config = replace(config, extensions=config.extensions + (other,))
    h = Harness(config)
    with pytest.raises(ValueError, match="冲突"):
        h.runtime.set_enabled(7, "disabled", True)
    h.runtime.start()
    h.change(10)
    h.runtime.release(7, "extension")
    h.tick(0)
    assert not h.writes


def copy_station(*, unconditional=False):
    config = station()
    rule = replace(config.extensions[0].rules[0],
                   source=None if unconditional else PointRef("HR", 0),
                   op="always" if unconditional else "eq",
                   steps=(SequenceStep(0, (ScenarioValue(PointRef("HR", 10), 0, PointRef("HR", 0)),)),
                          SequenceStep(2000, (ScenarioValue(PointRef("HR", 10), 0, PointRef("HR", 0)),))))
    extension = check_extension(config, replace(config.extensions[0], rules=(rule,)))
    return replace(config, extensions=(extension,))


def test_point_values_are_read_at_each_action_and_logged_as_actual_values():
    h = Harness(copy_station())
    h.runtime.start()
    h.change(10)
    h.tick(0)
    assert h.values[h.output] == 10
    h.change(37)
    h.tick(2)
    assert h.values[h.output] == 37
    assert any("HR/10 = 37" in e.result for e in h.events if e.action == "step")


def test_one_step_reads_all_point_values_before_writing_any_output():
    config = copy_station(unconditional=True)
    rule = replace(config.extensions[0].rules[0], steps=(SequenceStep(0, (
        ScenarioValue(PointRef("HR", 10), 9),
        ScenarioValue(PointRef("HR", 0), 0, PointRef("HR", 10)),
    )),))
    extension = check_extension(config, replace(config.extensions[0], rules=(rule,)))
    h = Harness(replace(config, extensions=(extension,)))
    h.values[h.output] = 4
    h.runtime.start()
    h.tick(0)
    assert h.values[h.output] == 9
    assert h.values[h.source] == 4
    assert len(h.writes) == 1


@pytest.mark.parametrize("bad_value", (65536, float("nan"), 1.25))
def test_invalid_current_point_value_fails_entire_step_without_partial_write(bad_value):
    config = copy_station(unconditional=True)
    rule = replace(config.extensions[0].rules[0], steps=(SequenceStep(0, (
        ScenarioValue(PointRef("Coil", 0), 1),
        ScenarioValue(PointRef("HR", 10), 0, PointRef("HR", 0)),
    )),))
    extension = check_extension(config, replace(config.extensions[0], rules=(rule,)))
    h = Harness(replace(config, extensions=(extension,)))
    h.values[h.source] = bad_value
    h.runtime.start()
    h.tick(0)
    assert not h.writes
    assert h.values[h.done] == 0
    assert not h.runtime.arbiter.owners
    assert any(e.action == "failed" for e in h.events)


def test_unconditional_starts_once_and_pause_resume_preserves_sequence():
    h = Harness(copy_station(unconditional=True))
    h.runtime.start()
    h.tick(0)
    assert len(h.writes) == 1
    h.change(23)
    h.tick(1)
    h.runtime.pause()
    h.tick(100)
    h.runtime.start()
    h.tick(101)
    assert h.values[h.output] == 23
    h.runtime.pause()
    h.runtime.start()
    h.tick(101)
    assert len(h.writes) == 2
    assert sum(e.action == "started" for e in h.events) == 1
    h.runtime.reset()
    h.runtime.start()
    h.tick(101)
    assert sum(e.action == "started" for e in h.events) == 2


def test_unconditional_rearms_on_enable_station_start_and_pause_before_first_tick():
    h = Harness(copy_station(unconditional=True))
    h.runtime.start()
    h.runtime.pause()  # No tick has consumed the first start yet.
    h.runtime.start()
    h.tick(0)
    assert len(h.writes) == 1
    h.runtime.set_enabled(7, "extension", False)
    h.runtime.set_enabled(7, "extension", True)
    h.tick(0)
    assert len(h.writes) == 2
    h.offline.add(7)
    h.runtime.station_changed(7, False)
    h.tick(2)
    assert len(h.writes) == 2
    h.offline.clear()
    h.runtime.station_changed(7, True)
    h.tick(2)
    assert len(h.writes) == 3


def test_copy_dependency_is_serialized_and_validated_without_unused_condition_source():
    config = copy_station(unconditional=True)
    extension = config.extensions[0]
    assert decode_extension(extension_record(extension)) == extension
    assert extension.rules[0].source is None
    with pytest.raises(ValueError, match="取值点位不存在"):
        replace(config, points=config.points[1:]).validate()
    with pytest.raises(ValueError, match="类型或字序"):
        replace(config, points=(replace(config.points[0], data_type=DataType.INT16), *config.points[1:])).validate()
    with pytest.raises(ValueError, match="必须是整数"):
        floating = replace(config, points=(replace(config.points[0], data_type=DataType.FLOAT32), *config.points[1:]))
        check_extension(floating, extension)


def test_fixed_value_extension_keeps_legacy_digest():
    import hashlib
    import json
    from dataclasses import asdict

    extension = station().extensions[0]
    legacy = asdict(extension)
    for rule in legacy["rules"]:
        for step in rule["steps"]:
            for item in step["values"]:
                item.pop("value_source")
    for key in ("id", "name", "enabled", "checked_digest"):
        legacy.pop(key)
    digest = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    assert extension_digest(extension) == digest


def test_sdk_supports_unconditional_and_point_values_in_worker():
    source = '''from all_protocol.scenario_sdk import Scene, Step
def build(scene: Scene):
    scene.always(id="copy").sequence([
        Step(at_ms=0, values={scene.point("HR", 10): scene.point("HR", 0)}),
    ])
'''
    rules = compile_source(source)
    assert rules[0].op == "always" and rules[0].source is None
    assert rules[0].steps[0].values[0].value_source == PointRef("HR", 0)
    check_extension(station(), ScenarioExtension("copy", "复制", rules, mode="python", source=source))
