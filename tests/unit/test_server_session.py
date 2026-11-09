import asyncio
from dataclasses import dataclass

import pytest

from all_protocol.application.server_contracts import (
    LifecycleAction, ListenerState, ListenerUpdate, RuntimeChange, ServerCapability, ServerLifecycle,
    ServerRegistration,
)
from all_protocol.application.server_session import ServerSession


@dataclass(frozen=True)
class SerialPlan:
    device: str
    fail_bind: bool = False
    pause_bind: bool = False


class SerialAdapter:
    def __init__(self, plan, callbacks):
        self._plan = plan
        self._callbacks = callbacks
        self.bind_started = asyncio.Event()
        self.open = False
        self.shutdowns = 0
        self.values = {"speed": 0}

    @property
    def endpoint(self):
        return self._plan.device

    def validate_plan(self):
        if not self.endpoint:
            raise ValueError("missing serial device")

    async def bind(self):
        self.open = True
        self.bind_started.set()
        if self._plan.fail_bind:
            raise OSError("device busy")
        if self._plan.pause_bind:
            await asyncio.Future()

    async def shutdown(self):
        self.shutdowns += 1
        self.open = False

    def read_current(self, target):
        return self.values[target]

    def write_current(self, target, value):
        self.values[target] = value
        self._callbacks.changed(target)


def test_serial_adapter_validation_failed_bind_and_restart():
    async def run():
        events, adapters = [], []

        def create(plan, callbacks):
            adapter = SerialAdapter(plan, callbacks)
            adapters.append(adapter)
            return adapter

        registration = ServerRegistration("serial-test", "Serial test", create, frozenset(ServerCapability))
        session = ServerSession(events.append, registration)
        await session.start(SerialPlan(""))
        assert not adapters[-1].bind_started.is_set()
        assert events[-1].payload.state == ListenerState.STOPPED

        await session.start(SerialPlan("COM7", fail_bind=True))
        assert not adapters[-1].open and adapters[-1].shutdowns == 1
        assert events[-2].payload.action == LifecycleAction.START_FAILED
        assert "device busy" in events[-1].payload.error
        with pytest.raises(ValueError, match="服务未监听"):
            await session.read_current("speed")

        await session.start(SerialPlan("COM7"))
        adapter = adapters[-1]
        assert events[-1].payload == ListenerUpdate(ListenerState.LISTENING)
        assert (events[-1].protocol, events[-1].endpoint) == ("serial-test", "COM7")
        await session.write_current("speed", 125.5)
        assert await session.read_current("speed") == 125.5
        assert events[-1].payload == RuntimeChange("speed")

        await asyncio.gather(session.stop(), session.stop())
        assert not adapter.open and adapter.shutdowns == 1
        assert events[-2].payload == ServerLifecycle(LifecycleAction.STOPPED, "已停止监听")
        assert events[-1].payload == ListenerUpdate(ListenerState.STOPPED)

    asyncio.run(run())


def test_cancelled_start_releases_adapter_and_respects_capabilities():
    async def run():
        events, created = [], asyncio.Future()

        def create(plan, callbacks):
            adapter = SerialAdapter(plan, callbacks)
            if not created.done():
                created.set_result(adapter)
            return adapter

        registration = ServerRegistration(
            "serial-test", "Serial test", create, frozenset({ServerCapability.READ_CURRENT}),
        )
        session = ServerSession(events.append, registration)
        start = asyncio.create_task(session.start(SerialPlan("COM7", pause_bind=True)))
        adapter = await created
        await adapter.bind_started.wait()
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start
        assert not adapter.open and adapter.shutdowns == 1

        await session.start(SerialPlan("COM8"))
        try:
            assert await session.read_current("speed") == 0
            with pytest.raises(ValueError, match="协议不支持"):
                await session.write_current("speed", 10)
        finally:
            await session.stop()

    asyncio.run(run())
