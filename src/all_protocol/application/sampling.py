"""Runtime samples independent of Qt and portable point definitions."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import math
from time import monotonic

from .contracts import ErrorKind, Value


class Quality(StrEnum):
    NEVER = "未采样"
    GOOD = "有效"
    TIMEOUT = "超时"
    DISCONNECTED = "断开"
    PROTOCOL = "协议失败"
    STALE = "过期"


@dataclass(frozen=True, slots=True)
class Sample:
    point_id: str
    raw_value: Value | None = None
    sampled_at: datetime | None = None
    last_success_at: datetime | None = None
    quality: Quality = Quality.NEVER
    request_id: int | None = None
    generation: int = 0
    detail: str = ""
    last_success_tick: float | None = None


class SampleStore:
    def __init__(self, *, clock=monotonic):
        self.clock = clock
        self.generation = 0
        self.samples: dict[tuple[str, str], Sample] = {}
        self._targets = {}
        self._lifetimes = {}

    def sync(self, tables):
        targets = {(table.id, point.id): point.target for table in tables for point in table.points}
        changed = set()
        for key, target in targets.items():
            if self._targets.get(key) != target:
                self.samples[key] = Sample(key[1], generation=self.generation)
                changed.add(key)
        for key in self.samples.keys() - targets.keys():
            del self.samples[key]
        self._targets = targets
        self._lifetimes = {table.id: table.stale_after_ms / 1000 for table in tables}
        return changed

    def set_generation(self, generation):
        if generation <= self.generation:
            return set()
        self.generation = generation
        return self.disconnect()

    def disconnect(self):
        changed = set()
        for key, sample in self.samples.items():
            if sample.quality != Quality.NEVER and sample.quality != Quality.DISCONNECTED:
                self.samples[key] = replace(sample, quality=Quality.DISCONNECTED, detail="连接已断开")
                changed.add(key)
        return changed

    def update(self, event):
        key = event.table_id, event.point_id
        update = event.update
        if update.generation != self.generation or self._targets.get(key) != update.command.target:
            return set()
        previous = self.samples[key]
        value = update.points[0].value if update.points and len(update.points) == 1 else None
        quality = {ErrorKind.TIMEOUT: Quality.TIMEOUT, ErrorKind.DISCONNECTED: Quality.DISCONNECTED}.get(
            update.entry.error, Quality.PROTOCOL)
        valid = update.entry.error is None and value is not None and not (isinstance(value, float) and not math.isfinite(value))
        self.samples[key] = Sample(
            event.point_id, value if valid else previous.raw_value, update.entry.time,
            update.entry.time if valid else previous.last_success_at, Quality.GOOD if valid else quality,
            update.entry.request_id, self.generation, (update.entry.detail or "响应未包含有效标量") if not valid else "",
            self.clock() if valid else previous.last_success_tick,
        )
        return {key}

    def expire(self):
        now = self.clock()
        changed = set()
        for key, sample in self.samples.items():
            if (sample.quality == Quality.GOOD and sample.last_success_tick is not None
                    and now - sample.last_success_tick >= self._lifetimes[key[0]]):
                self.samples[key] = replace(sample, quality=Quality.STALE)
                changed.add(key)
        return changed
