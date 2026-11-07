"""Deterministic request selection. No I/O, Qt, sleeps, or wall-clock dependencies."""

from collections import deque
from dataclasses import dataclass
from itertools import count
from math import floor
from time import monotonic

from .contracts import Command


@dataclass(frozen=True)
class Work:
    command: Command
    table_id: str = ""
    members: tuple[tuple[str, Command], ...] = ()


def table_work(table_id, commands):
    result = []
    for point_id, command in commands:
        if command.values is not None:
            raise ValueError("整表读取规划不允许写请求")
        merge = getattr(result[-1].command.target, "merged_with", None) if result else None
        target = merge(command.target) if merge is not None and command.values is None else None
        if target is not None:
            previous = result.pop()
            result.append(Work(Command(target), table_id, previous.members + ((point_id, command),)))
        else:
            result.append(Work(command, table_id, ((point_id, command),)))
    return tuple(result)


@dataclass
class Operation:
    id: int
    items: tuple[Work, ...]
    index: int = 0
    successful: bool = True


@dataclass
class PollGroup(Operation):
    key: str = ""
    interval: float = 1.0
    due: float = 0.0
    in_cycle: bool = False
    stop_on_error: bool = False


@dataclass(frozen=True)
class Selected:
    owner: Operation
    work: Work


class RequestScheduler:
    def __init__(self, *, clock=monotonic, capacity=32, group_capacity=64, manual_burst=3):
        self.clock, self.capacity, self.group_capacity, self.manual_burst = clock, capacity, group_capacity, manual_burst
        self.operations: dict[int, Operation] = {}
        self.groups: dict[str, PollGroup] = {}
        self._manual = deque()
        self._group_order = deque()
        self._ids = count(1)
        self._streak = 0
        self.active: Selected | None = None
        self.finished: list[tuple[int, bool]] = []

    def submit(self, items):
        if len(self.operations) >= self.capacity:
            raise ValueError(f"手动任务队列已满（{self.capacity}），请求未入队、未发送")
        operation = Operation(next(self._ids), tuple(items))
        if not operation.items:
            self.finished.append((operation.id, True))
        else:
            self.operations[operation.id] = operation
            self._manual.append(operation)
        return operation

    def start_group(self, key, items, interval_ms, *, stop_on_error=False):
        if type(interval_ms) is not int or not 200 <= interval_ms <= 30000:
            raise ValueError("轮询周期必须为 200–30000 ms")
        if key in self.groups:
            raise ValueError("该轮询组已启动，请先停止")
        if len(self.groups) >= self.group_capacity:
            raise ValueError(f"轮询组已达上限（{self.group_capacity}）")
        if not items or any(item.command.values is not None for item in items):
            raise ValueError("轮询组必须包含读取请求")
        group = PollGroup(next(self._ids), tuple(items), key=key, interval=interval_ms / 1000,
                          due=self.clock(), stop_on_error=stop_on_error)
        self.groups[key] = group
        self._group_order.append(key)
        return group

    def stop_group(self, key):
        group = self.groups.pop(key, None)
        if group is not None:
            self._group_order.remove(key)
            if self.active is None or self.active.owner is not group:
                self.finished.append((group.id, True))
        return group

    def cancel(self, operation_id):
        operation = self.operations.pop(operation_id, None)
        if operation is not None:
            self._manual = deque(item for item in self._manual if item is not operation)
        return operation

    def clear(self):
        operations = tuple(self.operations.values())
        groups = tuple(self.groups.values())
        self.operations.clear()
        self.groups.clear()
        self._manual.clear()
        self._group_order.clear()
        self._streak = 0
        return operations, groups

    def _ready(self):
        now = self.clock()
        return [self.groups[key] for key in self._group_order
                if self.groups[key].in_cycle or self.groups[key].due <= now]

    def take(self):
        if self.active is not None:
            return None
        ready = self._ready()
        if self._manual and (not ready or self._streak < self.manual_burst):
            owner = self._manual.popleft()
            self._streak += 1
        elif ready:
            owner = ready[0]
            while self._group_order[0] != owner.key:
                self._group_order.rotate(-1)
            self._group_order.rotate(-1)
            owner.in_cycle = True
            self._streak = 0
        else:
            return None
        item = Selected(owner, owner.items[owner.index])
        owner.index += 1
        self.active = item
        return item

    def complete(self, item, successful):
        if self.active is not item:
            raise ValueError("不能完成非活动请求")
        self.active = None
        owner = item.owner
        owner.successful &= successful
        if isinstance(owner, PollGroup):
            if self.groups.get(owner.key) is not owner:
                self.finished.append((owner.id, True))
            elif not successful and owner.stop_on_error:
                self.stop_group(owner.key)
            elif owner.index == len(owner.items):
                owner.index, owner.in_cycle = 0, False
                # Advance to the next future slot, skipping every missed cycle.
                elapsed = max(0.0, self.clock() - owner.due)
                owner.due += (floor(elapsed / owner.interval) + 1) * owner.interval
        elif owner.id in self.operations:
            if owner.index == len(owner.items):
                del self.operations[owner.id]
                self.finished.append((owner.id, owner.successful))
            else:
                self._manual.append(owner)

    def delay(self):
        if self._manual or any(group.in_cycle for group in self.groups.values()):
            return 0.0
        return max(0.0, min(group.due for group in self.groups.values()) - self.clock()) if self.groups else None
