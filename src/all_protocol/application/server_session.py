import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from itertools import count
from time import monotonic
from uuid import uuid4

from .contracts import Value
from .faults import FaultInjector, FaultRule
from .server_contracts import (
    LifecycleAction, ListenerState, ListenerUpdate, RequestMetadata, RuntimeChange,
    ServerAdapter, ServerCallbacks, ServerCapability, ServerEvent, ServerLifecycle, ServerRegistration,
)
from .simulation import TICK_SECONDS, Simulation, SimulationCommand, SimulationState, WriteSpan


class ServerSession:
    def __init__(self, emit: Callable[[ServerEvent], None], registration: ServerRegistration, *, clock=monotonic):
        self._emit = emit
        self._registration = registration
        self._adapter: ServerAdapter | None = None
        self._state = ListenerState.STOPPED
        self._endpoint = ""
        self._session_id = ""
        self._request_ids = count(1)
        self._lifecycle = asyncio.Lock()
        self._clock = clock
        self._simulation: Simulation | None = None
        self._simulation_task: asyncio.Task | None = None
        self._simulating_write = False
        self._faults = FaultInjector(clock, self._publish)

    def _publish(self, payload, time: datetime | None = None):
        self._emit(ServerEvent(self._registration.key, self._endpoint, time or datetime.now(), payload))

    def _update(self, state: ListenerState, error: str = ""):
        self._state = state
        self._publish(ListenerUpdate(state, error))

    def _request(self, time: datetime, metadata: RequestMetadata):
        self._publish(replace(metadata, session_id=self._session_id, request_id=next(self._request_ids)), time)

    def _changed(self, details: object):
        self._publish(RuntimeChange(details))

    def _written(self, span: WriteSpan):
        if self._simulation is not None and not self._simulating_write and self._simulation.take_over(span):
            self._publish(self._simulation.snapshot())

    def _write_simulated(self, target, value):
        self._simulating_write = True
        try:
            self._write_current(target, value)
        finally:
            self._simulating_write = False

    async def _run_simulation(self, simulation):
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if simulation.tick():
                self._publish(simulation.snapshot())

    async def _cancel_simulation_task(self):
        task, self._simulation_task = self._simulation_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _shutdown(self):
        await self._cancel_simulation_task()
        if self._adapter is not None:
            await self._adapter.shutdown()
            self._adapter = None
        self._simulation = None
        if self._faults.update.rule is not None:
            self._faults.configure(None)

    async def start(self, plan: object):
        async with self._lifecycle:
            if self._state != ListenerState.STOPPED:
                return
            self._update(ListenerState.STARTING)
            try:
                await self._shutdown()
                self._endpoint = ""
                self._session_id = str(uuid4())
                self._request_ids = count(1)
                callbacks = ServerCallbacks(self._request, self._changed, self._written, self._faults.claim)
                adapter = self._registration.create_adapter(plan, callbacks)
                self._endpoint = adapter.endpoint
                adapter.validate_plan()
                points = self._registration.simulation_points
                if points is not None and (configured := tuple(points(plan))):
                    self._simulation = Simulation(configured, self._clock, self._write_simulated)
                self._adapter = adapter
                await adapter.bind()
            except (Exception, asyncio.CancelledError) as error:
                detail = " · ".join(filter(None, ("启动失败", self._endpoint, str(error) or type(error).__name__)))
                try:
                    await self._shutdown()
                except Exception as cleanup_error:
                    detail += f" · 清理失败：{cleanup_error}"
                finally:
                    self._publish(ServerLifecycle(LifecycleAction.START_FAILED, detail))
                    self._update(ListenerState.STOPPED, detail)
                    if self._adapter is None:
                        self._endpoint = ""
                if not isinstance(error, (ValueError, OSError)):
                    raise
                return
            self._publish(ServerLifecycle(LifecycleAction.STARTED, f"启动监听 · {self._endpoint}"))
            self._update(ListenerState.LISTENING)
            if self._simulation is not None:
                self._publish(self._simulation.snapshot())

    async def stop(self):
        async with self._lifecycle:
            if self._adapter is None:
                return
            previous = self._state
            self._update(ListenerState.STOPPING)
            if self._simulation is not None:
                self._simulation.command(SimulationCommand.PAUSE)
            try:
                await self._shutdown()
            except (Exception, asyncio.CancelledError) as error:
                self._update(previous, f"停止失败 · {error}")
                if self._simulation is not None:
                    self._publish(self._simulation.snapshot())
                raise
            self._publish(ServerLifecycle(LifecycleAction.STOPPED, "已停止监听"))
            self._update(ListenerState.STOPPED)
            self._endpoint = ""

    def _active_adapter(self, capability: ServerCapability) -> ServerAdapter:
        if self._adapter is None or self._state != ListenerState.LISTENING:
            raise ValueError("服务未监听")
        if capability not in self._registration.capabilities:
            raise ValueError("协议不支持该运行值操作")
        return self._adapter

    async def read_current(self, target: object) -> Value:
        return self._active_adapter(ServerCapability.READ_CURRENT).read_current(target)

    def _write_current(self, target: object, value: Value):
        self._active_adapter(ServerCapability.WRITE_CURRENT).write_current(target, value)

    async def write_current(self, target: object, value: Value):
        self._write_current(target, value)

    async def edit_current(self, target: object, value: Value):
        try:
            await self.write_current(target, value)
        except ValueError as error:
            self._update(self._state, str(error))

    async def configure_fault(self, rule: FaultRule | None):
        if self._state == ListenerState.LISTENING:
            self._faults.configure(rule)

    async def control_simulation(self, command: SimulationCommand, target=None):
        simulation = self._simulation
        if self._state != ListenerState.LISTENING or simulation is None:
            return
        simulation.command(command, target)
        if simulation.state == SimulationState.RUNNING:
            simulation.tick()
            if self._simulation_task is None:
                self._simulation_task = asyncio.create_task(self._run_simulation(simulation))
            self._publish(simulation.snapshot())
        else:
            self._publish(simulation.snapshot())
            await self._cancel_simulation_task()
