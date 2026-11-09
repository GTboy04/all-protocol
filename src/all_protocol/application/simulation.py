import math
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from .contracts import Value


MIN_PERIOD_MS = 20
TICK_SECONDS = MIN_PERIOD_MS / 1000
MAX_TIME_MS = 2_147_483_647


class RuleKind(StrEnum):
    FIXED = "fixed"
    INCREMENT = "increment"
    HEARTBEAT = "heartbeat"
    RAMP = "ramp"
    SINE = "sine"
    TIMELINE = "timeline"


@dataclass(frozen=True, slots=True)
class Assignment:
    at_ms: int
    value: Value


@dataclass(frozen=True, slots=True)
class SimulationRule:
    kind: RuleKind
    start_ms: int = 0
    period_ms: int = 1000
    value: Value | None = None
    step: int | float = 1
    low: int | float | None = None
    high: int | float | None = None
    amplitude: int | float | None = None
    actions: tuple[Assignment, ...] = ()


def decode_rule(data) -> SimulationRule | None:
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("actions", []), list):
        raise ValueError("场景规则格式无效")
    try:
        actions = tuple(Assignment(**action) for action in data.get("actions", []))
        return SimulationRule(**(data | {"kind": RuleKind(data["kind"]), "actions": actions}))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("场景规则格式无效") from error


@dataclass(frozen=True, slots=True)
class WriteSpan:
    bank: tuple
    start: int
    end: int
    mask: int = 0xFF

    def overlaps(self, other):
        return (self.bank == other.bank and self.start < other.end and other.start < self.end
                and bool(self.mask & other.mask))


@dataclass(frozen=True, slots=True)
class SimulationPoint:
    target: object
    label: str
    initial: Value
    encode: Callable[[Value], object]
    span: WriteSpan
    floating: bool
    boolean: bool
    rule: SimulationRule | None


@dataclass(frozen=True, slots=True)
class Timeline:
    times: tuple[int, ...]
    actions: tuple[Assignment, ...]


type Waveform = Callable[[int], Value | None] | Timeline


def _time(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_TIME_MS:
        raise ValueError(f"时间必须为 {minimum}–{MAX_TIME_MS} 毫秒之间的整数")


def _number(value):
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    raise ValueError("请输入有限数值")


def compile_rule(point: SimulationPoint, rule: SimulationRule) -> Waveform:
    """Validate the rule against the point's codec before any runtime writes."""
    try:
        return _compile(point, rule)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{point.label} · {error}") from error


def _compile(point: SimulationPoint, rule: SimulationRule) -> Waveform:
    kind = RuleKind(rule.kind)
    _time(rule.start_ms)
    _time(rule.period_ms, minimum=MIN_PERIOD_MS)
    if kind == RuleKind.TIMELINE:
        if not rule.actions:
            raise ValueError("时间轴至少需要一个赋值动作")
        times = []
        for action in rule.actions:
            _time(action.at_ms)
            if times and action.at_ms <= times[-1]:
                raise ValueError("时间轴的时间必须严格递增")
            point.encode(action.value)
            times.append(action.at_ms)

        return Timeline(tuple(times), rule.actions)

    initial = point.initial if rule.value is None else rule.value
    point.encode(initial)
    if kind == RuleKind.FIXED:
        return lambda elapsed: initial if elapsed >= rule.start_ms else None

    if kind == RuleKind.HEARTBEAT:
        if not point.boolean:
            raise ValueError("心跳翻转需要 Bool 点位")

        def sample(elapsed):
            return int(initial) ^ (elapsed // rule.period_ms % 2)

    else:
        if point.boolean:
            raise ValueError("Bool 点位支持固定值、心跳和时间轴")
        sample = _numeric_waveform(point, rule, kind, _number(int(initial) if type(initial) is bool else initial))
    return lambda elapsed: sample(elapsed - rule.start_ms) if elapsed >= rule.start_ms else None


def _numeric_waveform(point, rule, kind, initial):
    if kind == RuleKind.SINE:
        amplitude = _number(rule.amplitude)
        if amplitude <= 0 or (not point.floating and type(amplitude) is not int):
            raise ValueError("幅度必须为正数，整数点位使用整数幅度")
        point.encode(initial - amplitude)
        point.encode(initial + amplitude)

        def sine(elapsed):
            factor = math.sin(math.tau * (elapsed % rule.period_ms) / rule.period_ms)
            offset = amplitude * factor if point.floating else round(amplitude * Fraction(factor))
            return initial + offset

        return sine

    high = _number(rule.high)
    point.encode(high)
    start, end = Fraction(initial), Fraction(high)
    convert = float if point.floating else round
    if kind == RuleKind.RAMP:
        return lambda elapsed: convert(start + (end - start) * Fraction(min(elapsed, rule.period_ms), rule.period_ms))

    low, step = _number(rule.low), _number(rule.step)
    point.encode(low)
    if not low <= initial <= high or step <= 0 or (not point.floating and type(step) is not int):
        raise ValueError("递增要求下限 ≤ 初值 ≤ 上限，步长为正数且符合点位类型")
    lower, stride = Fraction(low), Fraction(step)
    first_run = (end - start) // stride + 1
    cycle = (end - lower) // stride + 1

    def increment(elapsed):
        ticks = elapsed // rule.period_ms
        if ticks < first_run:
            return convert(start + ticks * stride)
        return convert(lower + ((ticks - first_run) % cycle) * stride)

    return increment


class SimulationState(StrEnum):
    READY = "就绪"
    RUNNING = "运行中"
    PAUSED = "已暂停"


class SimulationCommand(StrEnum):
    START = "start"
    PAUSE = "pause"
    RESET = "reset"
    RELEASE = "release"


@dataclass(frozen=True, slots=True)
class SimulationUpdate:
    state: SimulationState
    held: frozenset[object]
    faults: tuple[tuple[object, str], ...]


class Simulation:
    def __init__(self, points: tuple[SimulationPoint, ...], clock, write):
        self._points = {point.target: point for point in points}
        self._waveforms = {point.target: compile_rule(point, point.rule) for point in points}
        self._clock, self._write = clock, write
        self._last = {}
        self._timeline_next: dict[object, int | None] = {}
        self._held = set()
        self._faults = {}
        self._started = 0.0
        self._elapsed = 0.0
        self.state = SimulationState.READY

    def snapshot(self):
        return SimulationUpdate(self.state, frozenset(self._held), tuple(self._faults.items()))

    def take_over(self, span: WriteSpan) -> bool:
        previous = len(self._held)
        self._held.update(target for target, point in self._points.items() if point.span.overlaps(span))
        return len(self._held) != previous

    def command(self, command: SimulationCommand, target=None):
        if command == SimulationCommand.START and self.state != SimulationState.RUNNING:
            self._started = self._clock()
            self.state = SimulationState.RUNNING
        elif command == SimulationCommand.PAUSE and self.state == SimulationState.RUNNING:
            self._elapsed += self._clock() - self._started
            self.state = SimulationState.PAUSED
        elif command == SimulationCommand.RESET:
            self.state = SimulationState.READY
            self._elapsed = 0.0
            self._held.clear()
            self._faults.clear()
            self._last.clear()
            self._timeline_next.clear()
            for key, point in self._points.items():
                self._set_value(key, point.initial)
        elif command == SimulationCommand.RELEASE and target in self._held:
            self._held.remove(target)
            self._last.pop(target, None)
            self._timeline_next[target] = None

    def tick(self) -> bool:
        if self.state != SimulationState.RUNNING:
            return False
        elapsed = round((self._elapsed + (self._clock() - self._started)) * 1_000_000_000) // 1_000_000
        previous = len(self._faults)
        for target, waveform in self._waveforms.items():
            if target in self._held or target in self._faults:
                continue
            try:
                if isinstance(waveform, Timeline):
                    self._tick_timeline(target, waveform, elapsed)
                else:
                    self._apply(target, waveform(elapsed))
            except (ValueError, OverflowError) as error:
                self._fail(target, error)
        return len(self._faults) != previous

    def _tick_timeline(self, target, timeline, elapsed):
        start = self._timeline_next.get(target, 0)
        end = bisect_right(timeline.times, elapsed, lo=start or 0)
        # On release, restore the current setpoint without replaying actions missed during takeover.
        if start is None:
            start = max(0, end - 1)
        for index in range(start, end):
            self._apply(target, timeline.actions[index].value)
            if target in self._faults:
                break
        self._timeline_next[target] = end

    def _apply(self, target, value):
        if value is not None and value != self._last.get(target):
            self._set_value(target, value)

    def _set_value(self, target, value):
        try:
            self._write(target, value)
        except (ValueError, OverflowError) as error:
            self._fail(target, error)
        else:
            self._last[target] = value

    def _fail(self, target, error):
        self._faults[target] = f"{self._points[target].label} · {error}"
