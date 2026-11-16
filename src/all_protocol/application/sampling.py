"""Runtime samples independent of Qt and portable point definitions."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import math
from time import monotonic

from .contracts import ErrorKind, Value, ValueMetadata


class Quality(StrEnum):
    NEVER = "未采样"
    GOOD = "有效"
    TIMEOUT = "超时"
    DISCONNECTED = "断开"
    PROTOCOL = "协议失败"
    STALE = "过期"
    UNCERTAIN = "不确定"
    BAD = "无效"


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
    metadata: ValueMetadata | None = None
    changed_at: datetime | None = None


class SampleStore:
    def __init__(self, *, clock=monotonic):
        self.clock = clock
        self.generation = 0
        self.samples: dict[tuple[str, str], Sample] = {}
        self._targets = {}
        self._lifetimes = {}
        self.subscription_health = {}
        self._publish_ticks = {}
        self._subscription_values = set()

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
        for table_id in self.subscription_health.keys() - self._lifetimes.keys():
            self.subscription_health.pop(table_id, None)
            self._publish_ticks.pop(table_id, None)
        self._subscription_values.intersection_update(targets)
        return changed

    def set_generation(self, generation):
        if generation <= self.generation:
            return set()
        self.generation = generation
        return self.disconnect()

    def disconnect(self):
        self.subscription_health.clear()
        self._publish_ticks.clear()
        self._subscription_values.clear()
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
        metadata = update.points[0].metadata if update.points and len(update.points) == 1 else None
        detail = (update.entry.detail or "响应未包含有效标量") if not valid else ""
        if metadata is not None:
            valid = valid and metadata.quality == "Good"
            quality = Quality.UNCERTAIN if metadata.quality == "Uncertain" else Quality.BAD
            detail = update.entry.detail or metadata.summary
        self.samples[key] = Sample(
            event.point_id, value if valid else previous.raw_value, update.entry.time,
            update.entry.time if valid else previous.last_success_at, Quality.GOOD if valid else quality,
            update.entry.request_id, self.generation, detail,
            self.clock() if valid else previous.last_success_tick,
            metadata,
            update.entry.time if valid and (previous.raw_value is None or value != previous.raw_value) else previous.changed_at,
        )
        if update.entry.function == "DataChangeNotification":
            self._subscription_values.add(key)
        else:
            self._subscription_values.discard(key)
        return {key}

    def update_health(self, health):
        if health.generation != self.generation or health.table_id not in self._lifetimes:
            return
        if health.max_silence_ms == 0:
            self.stop_subscription(health.table_id)
            return
        previous = self.subscription_health.get(health.table_id)
        self.subscription_health[health.table_id] = health
        if health.last_publish_at is not None and (previous is None or previous.last_publish_at != health.last_publish_at):
            self._publish_ticks[health.table_id] = self.clock()

    def stop_subscription(self, table_id):
        self.subscription_health.pop(table_id, None)
        self._publish_ticks.pop(table_id, None)
        self._subscription_values = {key for key in self._subscription_values if key[0] != table_id}

    def expire(self):
        now = self.clock()
        changed = set()
        for key, sample in self.samples.items():
            health = self.subscription_health.get(key[0])
            published = self._publish_ticks.get(key[0])
            if (key in self._subscription_values and health is not None and health.alive
                    and published is not None and now - published < health.max_silence_ms / 1000):
                continue
            if (sample.quality == Quality.GOOD and sample.last_success_tick is not None
                    and now - sample.last_success_tick >= self._lifetimes[key[0]]):
                self.samples[key] = replace(sample, quality=Quality.STALE)
                changed.add(key)
        return changed
