import asyncio
from dataclasses import dataclass
from enum import StrEnum


class FaultAction(StrEnum):
    DELAY = "延迟应答"
    EXCEPTION = "异常应答"
    DISCONNECT = "主动断开"


class FaultOperation(StrEnum):
    ANY = "读写"
    READ = "读取"
    WRITE = "写入"


@dataclass(frozen=True, slots=True)
class FaultRule:
    action: FaultAction
    operation: FaultOperation = FaultOperation.ANY
    count: int | None = 1
    duration_ms: int | None = None
    delay_ms: int = 500

    def __post_init__(self):
        if (self.count is None) == (self.duration_ms is None):
            raise ValueError("选择次数或持续时间")
        if self.count is not None and not 1 <= self.count <= 10000:
            raise ValueError("次数须在 1 至 10000 之间")
        if self.duration_ms is not None and not 1 <= self.duration_ms <= 600000:
            raise ValueError("持续时间须在 1 至 600000 ms 之间")
        if self.action == FaultAction.DELAY and not 1 <= self.delay_ms <= 60000:
            raise ValueError("延迟须在 1 至 60000 ms 之间")


@dataclass(frozen=True, slots=True)
class FaultUpdate:
    rule: FaultRule | None = None
    triggered: int = 0
    remaining: int | None = None
    active: bool = False


@dataclass(frozen=True, slots=True)
class FaultHit:
    rule: FaultRule
    released: asyncio.Event

    async def wait(self):
        try:
            async with asyncio.timeout(self.rule.delay_ms / 1000):
                await self.released.wait()
        except TimeoutError:
            pass


class FaultInjector:
    """A rule shared by the listener's connections, claimed on its I/O loop."""

    def __init__(self, clock, publish):
        self._clock = clock
        self._publish = publish
        self.update = FaultUpdate()
        self._released = asyncio.Event()
        self._deadline = None
        self._expiry = None

    def configure(self, rule: FaultRule | None):
        if self._expiry is not None:
            self._expiry.cancel()
        self._released.set()
        self._released = asyncio.Event()
        self._expiry = self._deadline = None
        if rule is not None and rule.duration_ms is not None:
            seconds = rule.duration_ms / 1000
            self._deadline = self._clock() + seconds
            self._expiry = asyncio.get_running_loop().call_later(seconds, self._expire)
        self._set(FaultUpdate(rule, remaining=rule.count if rule else None, active=rule is not None))

    def _set(self, update: FaultUpdate):
        self.update = update
        self._publish(update)

    def _expire(self):
        if self._expiry is not None:
            self._expiry.cancel()
        self._expiry = self._deadline = None
        if self.update.active:
            self._set(FaultUpdate(self.update.rule, self.update.triggered))

    def claim(self, operation: FaultOperation) -> FaultHit | None:
        if self._deadline is not None and self._clock() >= self._deadline:
            self._expire()
        rule = self.update.rule
        if not self.update.active or rule.operation not in (FaultOperation.ANY, operation):
            return None
        remaining = self.update.remaining
        if remaining is not None:
            remaining -= 1
        self._set(FaultUpdate(rule, self.update.triggered + 1, remaining, remaining != 0))
        return FaultHit(rule, self._released)
