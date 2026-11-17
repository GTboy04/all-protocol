"""Single-loop edge triggering and deterministic delayed sequences."""
import heapq
import math
from collections import deque
from dataclasses import dataclass
from itertools import count

from .scenario_model import ScenarioEvent, matches
from .write_arbiter import WriteArbiter


@dataclass(frozen=True, slots=True)
class ScenarioStatus:
    unit_id: int
    extension_id: str
    rule_id: str
    state: str
    step: int = 0
    remaining_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ScenarioUpdate:
    statuses: tuple[ScenarioStatus, ...]


@dataclass
class _Rule:
    unit: int
    extension: object
    rule: object
    source: object
    steps: tuple
    targets: frozenset
    enabled: bool
    baseline: bool | None = None
    run: int = 0
    started: float = 0
    index: int = 0
    state: str = "就绪"
    automatic_started: bool = False


class ScenarioRuntime:
    def __init__(self, bindings, points, clock, read, write_batch, active, emit, restore):
        self.clock, self.read, self.write = clock, read, write_batch
        self.active, self.emit, self.restore = active, emit, restore
        self.arbiter = WriteArbiter(points)
        self.points = {p.target: p for p in points}
        self.rules = {}
        for unit, extension, rule, source, steps in bindings:
            key = unit, extension.id, rule.id
            self.rules[key] = _Rule(unit, extension, rule, source, steps,
                                    frozenset(t for _, values in steps for t, _, _ in values), extension.enabled)
        self.running = False
        self.elapsed = 0.0
        self.started = 0.0
        self.heap, self.pending = [], deque()
        self.ids = count(1)
        self.pending_reset = set()
        self.has_started = False
        self.station_generations = dict.fromkeys((item.unit for item in self.rules.values()), 0)

    def now(self):
        return self.elapsed + (self.clock() - self.started if self.running else 0)

    def _event(self, item, action, message, *, planned=None, sequence=None):
        self.emit(ScenarioEvent(item.unit, item.extension.id, item.rule.id, item.run, action,
                                f"Unit {item.unit} · {item.extension.name}/{item.rule.id} · {message}",
                                item.index, planned, round((self.now() - item.started) * 1000), sequence,
                                station_generation=self.station_generations[item.unit]))

    def snapshot(self):
        now = self.now()
        return ScenarioUpdate(tuple(ScenarioStatus(
            item.unit, item.extension.id, item.rule.id,
            ("异常，已停用" if item.state == "异常" and not item.enabled else "已停用" if not item.enabled else "从站未启动" if not self.active(item.source) else
             f"已暂停（{item.state}）" if not self.running and item.state == "执行中" else item.state), item.index,
            max(0, round(item.started * 1000 + item.steps[item.index][0] - now * 1000))
            if item.state == "执行中" and item.index < len(item.steps) else None,
        ) for item in self.rules.values()))

    def _baseline(self, item, *, initial=False):
        item.baseline = None
        if not item.enabled or not self.active(item.source):
            return
        if item.rule.op == "always":
            item.baseline = True
            if self.running and not item.automatic_started and not any(entry[0] is item for entry in self.pending):
                self.pending.append((item, self.now(), None))
            return
        item.baseline = matches(item.rule, self.read(item.source))
        if initial and item.rule.fire_initial and item.baseline:
            self.pending.append((item, self.now(), None))

    def start(self):
        if self.running:
            return
        initial = not self.has_started
        self.has_started = True
        self.started = self.clock()
        self.running = True
        for item in self.rules.values():
            self._baseline(item, initial=initial)

    def pause(self):
        if self.running:
            self.elapsed = self.now()
            self.running = False
        self.pending.clear()

    def committed(self, commit, origin):
        if not self.running or origin not in ("manual", "base"):
            return
        changes = {target: (before, after) for target, before, after in commit.changes}
        for item in self.rules.values():
            if item.rule.op == "always" or not item.enabled or not self.active(item.source) or item.source not in changes:
                continue
            before, after = changes[item.source]
            condition = matches(item.rule, after)
            previous = item.baseline
            item.baseline = condition
            if before == after:
                continue
            if item.rule.cancel_on_false and not condition:
                self.cancel(item, "条件失效")
            if condition and previous is False:
                self.pending.append((item, self.now(), commit.sequence))
                self.emit(ScenarioEvent(item.unit, item.extension.id, item.rule.id, item.run, "triggered",
                                        f"Unit {item.unit} · {item.extension.name}/{item.rule.id} · "
                                        f"{item.rule.source.area}/{item.rule.source.offset}: {before} → {after}",
                                        commit_sequence=commit.sequence,
                                        source=f"{item.rule.source.area}/{item.rule.source.offset}",
                                        source_before=str(before) if type(before) is float and not math.isfinite(before) else before,
                                        source_after=after, station_generation=self.station_generations[item.unit]))
        if len(self.pending) > 4096:
            self.pending.clear()
            first = next((item for item in self.rules.values() if item.enabled), None)
            if first is not None:
                self._event(first, "failed", "触发队列溢出，已停用所有扩展")
            for item in self.rules.values():
                if item.enabled:
                    self.cancel(item, "触发队列溢出，已停用扩展")
                    item.enabled = False

    def manual(self, span):
        for key in self.arbiter.take_over(span):
            self.cancel(self.rules[key], "手动/客户端写入接管")

    def _release(self, item):
        key = item.unit, item.extension.id, item.rule.id
        for target in self.arbiter.release(key):
            self.restore(target)

    def cancel(self, item, reason):
        previous = item.state
        item.state = "已取消"
        self.pending = deque(entry for entry in self.pending if entry[0] is not item)
        self.heap[:] = [entry for entry in self.heap if entry[1] != item.run]
        heapq.heapify(self.heap)
        self._release(item)
        if previous in ("执行中", "已完成，保持输出"):
            self._event(item, "cancelled", reason)

    def release(self, unit, extension_id):
        for item in self.rules.values():
            if (item.unit, item.extension.id) == (unit, extension_id):
                self.cancel(item, "释放扩展控制，恢复基础场景")
                self._event(item, "released", "已释放输出")

    def set_enabled(self, unit, extension_id, enabled):
        selected = [item for item in self.rules.values() if (item.unit, item.extension.id) == (unit, extension_id)]
        if enabled:
            for item in selected:
                if any(item.targets & other.targets for other in self.rules.values()
                       if other is not item and (other.enabled or other in selected)):
                    raise ValueError("该扩展与已启用扩展的输出范围冲突，请先停用其他扩展")
        for item in self.rules.values():
            if (item.unit, item.extension.id) == (unit, extension_id):
                if not enabled:
                    self.cancel(item, "停用扩展")
                    item.automatic_started = False
                item.enabled = enabled
                self._baseline(item)
                self._event(item, "enabled" if enabled else "disabled", "启用扩展" if enabled else "停用扩展")

    def station_changed(self, unit, active):
        self.station_generations[unit] = self.station_generations.get(unit, 0) + 1
        self.pending = deque(entry for entry in self.pending if entry[0].unit != unit)
        if active:
            values = tuple((target, self.points[target].initial) for target in self.pending_reset
                           if target.unit_id == unit)
            if values:
                self.write(values)
                self.pending_reset.difference_update(target for target, _ in values)
        for item in self.rules.values():
            if item.unit == unit:
                if not active:
                    self.cancel(item, "从站已停止")
                    item.automatic_started = False
                self._baseline(item)

    def reset(self):
        self.pause()
        self.has_started = False
        self.heap.clear()
        for item in self.rules.values():
            self.cancel(item, "重置场景")
            item.state, item.baseline = "就绪", None
            item.automatic_started = False
        self.elapsed = 0
        self.arbiter.reset()
        outputs = {target for item in self.rules.values() for target in item.targets}
        self.pending_reset.update(target for target in outputs if not self.active(target))
        values = tuple((target, self.points[target].initial) for target in outputs if self.active(target))
        if values:
            self.write(values)

    def close(self):
        self.pause()
        self.heap.clear()
        for item in self.rules.values():
            self.cancel(item, "服务已停止")

    def tick(self):
        if not self.running:
            return
        now = self.now()
        while self.pending:
            item, started, sequence = self.pending.popleft()
            if not item.enabled or not self.active(item.source):
                continue
            if item.rule.op == "always":
                item.automatic_started = True
            if item.state == "执行中":
                self._event(item, "ignored", "执行中，忽略触发", sequence=sequence)
                continue
            key = item.unit, item.extension.id, item.rule.id
            if (sum(r.state == "执行中" for r in self.rules.values()) >= 256
                    or not all(self.active(t) for t in item.targets)
                    or not self.arbiter.claim(key, item.targets)):
                self._event(item, "rejected", "输出已被接管、从站未启动或达到执行上限", sequence=sequence)
                continue
            item.run, item.started, item.index, item.state = next(self.ids), started, 0, "执行中"
            heapq.heappush(self.heap, (started + item.steps[0][0] / 1000, item.run, key, 0))
            self._event(item, "started", "无条件，开始执行" if item.rule.op == "always" else "条件满足，开始执行", sequence=sequence)
        while self.heap and self.heap[0][0] <= now + 1e-9:
            due, run, key, index = heapq.heappop(self.heap)
            item = self.rules[key]
            if item.run != run or item.state != "执行中":
                continue
            if not self.active(item.source):
                self.cancel(item, "从站未启动")
                continue
            if now - due > 1.0:
                self.cancel(item, "动作延迟超过 1000 ms，取消后续步骤")
                continue
            try:
                # Resolve all reads before committing any output in the same step.
                values = tuple((target, self.read(source) if source is not None else value)
                               for target, value, source in item.steps[index][1])
                for target, value in values:
                    if type(value) not in (bool, int, float) or (type(value) is float and not math.isfinite(value)):
                        raise ValueError("取值点位的当前值必须为有限数值")
                    self.points[target].encode(value)
                self.write(values)
            except (ValueError, OverflowError) as error:
                self.cancel(item, f"动作失败：{error}")
                item.state = "异常"
                item.enabled = False
                for other in self.rules.values():
                    if other is not item and (other.unit, other.extension.id) == (item.unit, item.extension.id):
                        self.cancel(other, "扩展内其他规则报错，已停用")
                        other.enabled = False
                self._event(item, "failed", str(error))
                continue
            item.index = index + 1
            description = ", ".join(f"{target.area}/{target.offset} = {value}" for target, value in values)
            self._event(item, "step", f"步骤 {item.index} 已提交：{description}", planned=item.steps[index][0])
            if item.index == len(item.steps):
                item.state = "已完成，保持输出" if item.rule.completion == "hold" else "已完成，已释放"
                if item.rule.completion == "release":
                    self._release(item)
                self._event(item, "completed", item.state)
            else:
                heapq.heappush(self.heap, (item.started + item.steps[item.index][0] / 1000, run, key, item.index))
