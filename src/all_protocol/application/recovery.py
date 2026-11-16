"""Portable, opt-in recovery settings; retry counters are runtime-only."""

from dataclasses import dataclass


PREFIX = "recovery_"


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    enabled: bool = False
    interval_ms: int = 1000
    max_interval_ms: int = 30000
    max_attempts: int = 5
    restore_reads: bool = False

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.restore_reads) is not bool:
            raise ValueError("恢复开关必须为布尔值")
        if (type(self.interval_ms) is not int or type(self.max_interval_ms) is not int
                or not 200 <= self.interval_ms <= self.max_interval_ms <= 300000):
            raise ValueError("重试间隔须在 200–300000 ms，最大间隔不得小于初始间隔")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 100:
            raise ValueError("重试上限须为 1–100 次")

    def delay(self, attempt):
        return min(self.max_interval_ms, self.interval_ms * 2 ** (attempt - 1)) / 1000

    def fields(self):
        if self == RecoveryPolicy():
            return {}
        return {PREFIX + key: int(value) for key, value in (
            ("enabled", self.enabled), ("interval_ms", self.interval_ms),
            ("max_interval_ms", self.max_interval_ms), ("max_attempts", self.max_attempts),
            ("restore_reads", self.restore_reads))}

    @classmethod
    def from_fields(cls, fields):
        if not isinstance(fields, dict) or any(not isinstance(key, str) for key in fields):
            raise ValueError("设备字段必须是以文本命名的对象")
        allowed = {PREFIX + key for key in cls.__dataclass_fields__}
        if any(key.startswith(PREFIX) and key not in allowed for key in fields):
            raise ValueError("未知恢复配置字段")
        values = {key: fields[PREFIX + key] for key in cls.__dataclass_fields__ if PREFIX + key in fields}
        for key in ("enabled", "restore_reads"):
            if key in values:
                if type(values[key]) is not int or values[key] not in (0, 1):
                    raise ValueError("恢复开关须为 0 或 1")
                values[key] = bool(values[key])
        return cls(**values)
