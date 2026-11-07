import asyncio
from dataclasses import dataclass

from all_protocol.application.contracts import (
    ClientError, Command, ErrorKind, JournalEntry, OperationRejected, Point, ProtocolRegistration, SessionState, TableReadUpdate,
)
from all_protocol.application.session import Session


@dataclass(frozen=True)
class Target:
    label: str


class Time:
    def __init__(self):
        self.now = 0.0
        self.idle = asyncio.Event()

    def __call__(self):
        return self.now

    async def wait(self, wake, _delay):
        self.idle.set()
        await wake.wait()


def test_groups_continue_after_timeout_with_virtual_time_and_stop_independently():
    async def run():
        time = Time()
        events, calls = [], []

        class Client:
            async def connect(self):
                pass

            async def close(self):
                pass

            def function(self, _command):
                return "read"

            async def read(self, target):
                calls.append(target.label)
                if target.label == "bad":
                    raise ClientError(ErrorKind.TIMEOUT, "超时")
                return (Point("p", "value", 1),)

        session = Session(events.append, clock=time, wait=time.wait)
        await session.connect(ProtocolRegistration("fake", "Fake", lambda *_: Client()), None, "fake")
        try:
            await session.start_table_polling("a", (("bad", Command(Target("bad"))),), 200)
            await session.start_table_polling("b", (("good", Command(Target("good"))),), 400)
            await asyncio.wait_for(time.idle.wait(), 1)
            assert calls == ["bad", "good"]
            assert set(session._scheduler.groups) == {"a", "b"}
            time.idle.clear()
            time.now = .2
            session._wake.set()
            await asyncio.wait_for(time.idle.wait(), 1)
            assert calls == ["bad", "good", "bad"]
            await session.stop_table_polling("a")
            time.idle.clear()
            time.now = 10
            session._wake.set()
            await asyncio.wait_for(time.idle.wait(), 1)
            assert calls == ["bad", "good", "bad", "good"]
            assert session._scheduler.groups["b"].due > time.now
            updates = [event for event in events if isinstance(event, TableReadUpdate)]
            assert updates[0].update.entry.error == ErrorKind.TIMEOUT
            assert updates[1].update.points[0].value == 1
        finally:
            await session.disconnect()
        assert not session._waiters and not session._scheduler.groups
    asyncio.run(run())


def test_inflight_poll_finishes_then_manual_requests_get_bounded_priority():
    async def run():
        first_started, release, queue_full = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        def emit(event):
            if isinstance(event, SessionState) and event.queued == 5:
                queue_full.set()

        class Client:
            async def connect(self):
                pass

            async def close(self):
                pass

            def function(self, command):
                return command.operation

            async def read(self, target):
                calls.append(target.label)
                if target.label == "p0":
                    first_started.set()
                    await release.wait()
                return (Point(target.label, "value", 1),)

            async def write(self, target, _values):
                calls.append(target.label)

        session = Session(emit)
        await session.connect(ProtocolRegistration("fake", "Fake", lambda *_: Client(), True), None, "fake")
        try:
            await session.start_table_polling("g", (("p0", Command(Target("p0"))), ("p1", Command(Target("p1")))), 30000)
            await first_started.wait()
            tasks = [asyncio.create_task(session.execute(Command(Target(f"w{i}"), (i,)))) for i in range(5)]
            await asyncio.wait_for(queue_full.wait(), 1)
            assert calls == ["p0"]
            release.set()
            await asyncio.gather(*tasks)
            assert calls == ["p0", "w0", "w1", "w2", "p1", "w3", "w4"]
        finally:
            await session.disconnect()
    asyncio.run(run())


def test_queue_full_is_explicit_and_disconnect_logs_unsent_write_without_replaying_it():
    async def run():
        started, admitted = asyncio.Event(), asyncio.Event()
        events, calls = [], []

        def emit(event):
            events.append(event)
            if isinstance(event, SessionState) and event.queued == 2:
                admitted.set()

        class Client:
            async def connect(self):
                pass

            async def close(self):
                pass

            def function(self, command):
                return command.operation

            async def read(self, target):
                calls.append(target.label)
                started.set()
                await asyncio.Event().wait()

            async def write(self, target, values):
                calls.append("write")

        session = Session(emit, queue_capacity=2)
        registration = ProtocolRegistration("fake", "Fake", lambda *_: Client(), True)
        await session.connect(registration, None, "fake")
        read = asyncio.create_task(session.execute(Command(Target("blocked"))))
        await started.wait()
        write = asyncio.create_task(session.execute(Command(Target("queued"), (42,))))
        await admitted.wait()
        assert await session.execute(Command(Target("overflow"), (99,))) is False
        notice = next(event for event in events if isinstance(event, OperationRejected))
        assert notice.operation == "写入" and "未发送" in notice.detail
        await session.disconnect()
        results = await asyncio.gather(read, write, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        cancelled = next(event for event in events if isinstance(event, JournalEntry) and event.target == "queued")
        assert cancelled.outcome == "已取消（未发送）" and not cancelled.packets and cancelled.operation_id is not None
        await session.connect(registration, None, "fake")
        await session.disconnect()
        assert calls == ["blocked"]
    asyncio.run(run())


def test_stop_group_keeps_active_read_and_discards_rest_of_cycle():
    async def run():
        started, release, idle = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls, events = [], []

        class Client:
            async def connect(self):
                pass

            async def close(self):
                pass

            def function(self, command):
                return "read"

            async def read(self, target):
                calls.append(target.label)
                started.set()
                await release.wait()
                return (Point("p", "value", 1),)

        async def wait(wake, _delay):
            idle.set()
            await wake.wait()

        session = Session(events.append, wait=wait)
        await session.connect(ProtocolRegistration("fake", "Fake", lambda *_: Client()), None, "fake")
        try:
            await session.start_table_polling("g", (("p0", Command(Target("p0"))), ("p1", Command(Target("p1")))), 200)
            await started.wait()
            await session.stop_table_polling("g")
            assert session._scheduler.active is not None
            release.set()
            await idle.wait()
            assert calls == ["p0"]
            assert next(event for event in events if isinstance(event, TableReadUpdate)).update.points[0].value == 1
        finally:
            await session.disconnect()
    asyncio.run(run())
