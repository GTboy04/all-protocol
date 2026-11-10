from dataclasses import dataclass
from datetime import datetime


VISIBLE_LOG_ROWS = 500


@dataclass(frozen=True, slots=True)
class LogRetention:
    rotated_at: datetime | None = None
    clipped: bool = False
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class RetainedLog[T]:
    entries: tuple[T, ...] = ()
    retention: LogRetention = LogRetention()
