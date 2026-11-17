import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from itertools import count
from time import monotonic
from typing import cast
from uuid import uuid4

from .contracts import Value
from .faults import FaultInjector, FaultRule
from .server_contracts import (
    LifecycleAction, ListenerState, ListenerUpdate, RequestMetadata, RuntimeChange,
    ServerAdapter, ServerCallbacks, ServerCapability, ServerEvent, ServerLifecycle, ServerRegistration,
    StationControlledServerAdapter,
    ScenarioServerAdapter,
)
from .simulation import TICK_SECONDS, Simulation, SimulationCommand, SimulationState, WriteSpan
from .scenario_runtime import ScenarioRuntime
from .scenario_model import ScenarioEvent, extension_digest
from ..scenario_trust import is_trusted


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
        self._write_origin = "manual"
        self._scenario: ScenarioRuntime | None = None
        self._scenario_scheduled = False
        self._reset_points = ()
        self._pending_point_resets = {}
        self._faults = FaultInjector(clock, self._publish)

    def _publish(self, payload, time: datetime | None = None):
        if isinstance(payload, ScenarioEvent):
            payload = replace(payload, session_id=self._session_id)
        self._emit(ServerEvent(self._registration.key, self._endpoint, time or datetime.now(), payload))

    def _update(self, state: ListenerState, error: str = ""):
        self._state = state
        self._publish(ListenerUpdate(state, error))

    def _request(self, time: datetime, metadata: RequestMetadata):
        self._publish(replace(metadata, session_id=self._session_id, request_id=next(self._request_ids)), time)

    def _changed(self, details: object):
        self._publish(RuntimeChange(details))

    def _written(self, span: WriteSpan):
        changed = False
        if self._scenario is not None and self._write_origin == "manual":
            self._scenario.manual(span)
            changed = True
        if self._simulation is not None and self._write_origin == "manual":
            changed |= self._simulation.take_over(span)
        if changed and self._simulation is not None:
            self._publish(self._simulation_snapshot())

    def _simulation_snapshot(self):
        snapshot = self._simulation.snapshot()
        if self._scenario is not None:
            snapshot = replace(snapshot, held=snapshot.held | frozenset(self._scenario.arbiter.manual))
        return snapshot

    def _committed(self, commit):
        if self._scenario is not None:
            self._scenario.committed(commit, self._write_origin)
            if self._scenario.pending and not self._scenario_scheduled:
                self._scenario_scheduled = True
                asyncio.get_running_loop().call_soon(self._drain_scenarios, self._scenario)

    def _drain_scenarios(self, scenario):
        self._scenario_scheduled = False
        if scenario is self._scenario:
            scenario.tick()
            self._publish(scenario.snapshot())

    def _write_scenario(self, values):
        previous, self._write_origin = self._write_origin, "extension"
        try:
            cast(ScenarioServerAdapter, self._adapter).write_batch(values)
        finally:
            self._write_origin = previous

    def _write_simulated(self, target, value):
        previous = self._write_origin
        self._write_origin = "reset" if previous == "reset" else "base"
        try:
            self._write_current(target, value)
        finally:
            self._write_origin = previous

    async def _run_simulation(self, simulation):
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if simulation.tick():
                self._publish(self._simulation_snapshot())
            if self._scenario is not None:
                self._scenario.tick()
                self._publish(self._scenario.snapshot())

    async def _cancel_simulation_task(self):
        task, self._simulation_task = self._simulation_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _shutdown(self):
        await self._cancel_simulation_task()
        if self._scenario is not None:
            self._scenario.close()
            self._scenario = None
        self._scenario_scheduled = False
        if self._adapter is not None:
            await self._adapter.shutdown()
            self._adapter = None
        self._simulation = None
        self._reset_points = ()
        self._pending_point_resets.clear()
        if self._faults.update.rule is not None:
            self._faults.configure(None)

    async def start(self, plan: object, *, stations: frozenset[int] | None = None):
        async with self._lifecycle:
            if self._state != ListenerState.STOPPED:
                return
            self._update(ListenerState.STARTING)
            try:
                await self._shutdown()
                self._endpoint = ""
                self._session_id = str(uuid4())
                self._request_ids = count(1)
                callbacks = ServerCallbacks(self._request, self._changed, self._written, self._faults.claim, self._committed)
                adapter = self._registration.create_adapter(plan, callbacks)
                self._endpoint = adapter.endpoint
                adapter.validate_plan()
                if self._registration.reset_points is not None:
                    self._reset_points = tuple(self._registration.reset_points(plan))
                if stations is not None:
                    if ServerCapability.STATION_CONTROL not in self._registration.capabilities:
                        raise ValueError("协议不支持独立启停从站")
                    cast(StationControlledServerAdapter, adapter).configure_stations(stations)
                points = self._registration.simulation_points
                configured = tuple(points(plan)) if points is not None else ()
                bindings = tuple(self._registration.scenario_bindings(plan)) if self._registration.scenario_bindings else ()
                if configured or bindings:
                    self._simulation = Simulation(configured, self._clock, self._write_simulated, enabled=self._base_enabled)
                self._adapter = adapter
                await adapter.bind()
                if bindings:
                    self._scenario = ScenarioRuntime(bindings, tuple(plan.simulation_points()), self._clock,
                                                     adapter.read_current, self._write_scenario, self._target_active,
                                                     self._publish, self._simulation.invalidate)
                    for item in self._scenario.rules.values():
                        if item.enabled and not is_trusted(item.extension):
                            item.enabled = False
                            self._scenario._event(item, "disabled", "本机尚未检查并启用此 Python 扩展")
                    self._publish(self._scenario.snapshot())
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
                self._publish(self._simulation_snapshot())

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
                    self._publish(self._simulation_snapshot())
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

    async def reset_points(self):
        """Restore configured initial values while keeping the listener open."""
        async with self._lifecycle:
            try:
                self._active_adapter(ServerCapability.WRITE_CURRENT)
                if self._registration.reset_points is None:
                    raise ValueError("协议不支持重置点位")
                # Reset stops timed/conditional writes and clears manual ownership.
                await self.control_simulation(SimulationCommand.RESET)
                previous, self._write_origin = self._write_origin, "reset"
                try:
                    for point in self._reset_points:
                        if self._target_active(point.target):
                            self._write_current(point.target, point.initial)
                        else:
                            self._pending_point_resets[point.target] = point.initial
                finally:
                    self._write_origin = previous
                detail = f"已重置 {len(self._reset_points)} 个点位为配置初始值"
                if self._pending_point_resets:
                    detail += f" · {len(self._pending_point_resets)} 个点位将在从站启动时恢复"
                self._publish(ServerLifecycle(LifecycleAction.POINTS_RESET, detail))
                self._update(self._state)
            except ValueError as error:
                self._update(self._state, f"重置点位失败 · {error}")

    async def configure_fault(self, rule: FaultRule | None):
        if self._state == ListenerState.LISTENING:
            self._faults.configure(rule)

    async def control_simulation(self, command: SimulationCommand, target=None):
        simulation = self._simulation
        if self._state != ListenerState.LISTENING or simulation is None:
            return
        scenario = self._scenario
        if scenario is not None:
            if command == SimulationCommand.START:
                scenario.start()
            elif command == SimulationCommand.PAUSE:
                scenario.pause()
            elif command == SimulationCommand.RESET:
                scenario.reset()
            elif command == SimulationCommand.RELEASE:
                scenario.arbiter.manual.discard(target)
        previous = self._write_origin
        if command == SimulationCommand.RESET:
            self._write_origin = "reset"
        try:
            simulation.command(command, target)
        finally:
            self._write_origin = previous
        if simulation.state == SimulationState.RUNNING:
            simulation.tick()
            if scenario is not None:
                scenario.tick()
            if self._simulation_task is None:
                self._simulation_task = asyncio.create_task(self._run_simulation(simulation))
            self._publish(self._simulation_snapshot())
        else:
            self._publish(self._simulation_snapshot())
            await self._cancel_simulation_task()
        if scenario is not None:
            self._publish(scenario.snapshot())

    def _base_enabled(self, target):
        return self._target_active(target) and (self._scenario is None or target not in self._scenario.arbiter.owners)

    async def control_extension(self, unit_id, extension_id, action):
        if self._scenario is None:
            return
        if action == "release":
            self._scenario.release(unit_id, extension_id)
        elif action in ("enable", "disable"):
            if action == "enable":
                for item in self._scenario.rules.values():
                    if ((item.unit, item.extension.id) == (unit_id, extension_id)
                            and (not is_trusted(item.extension)
                                 or item.extension.checked_digest != extension_digest(item.extension))):
                        self._scenario._event(item, "rejected", "请停止服务，检查并应用 Python 扩展")
                        return
            try:
                self._scenario.set_enabled(unit_id, extension_id, action == "enable")
            except ValueError as error:
                self._update(self._state, str(error))
        self._publish(self._scenario.snapshot())

    def _target_active(self, target):
        if ServerCapability.STATION_CONTROL in self._registration.capabilities:
            return self._adapter is not None and cast(StationControlledServerAdapter, self._adapter).is_target_active(target)
        return True

    async def set_station_active(self, unit_id: int, active: bool):
        async with self._lifecycle:
            try:
                adapter = cast(StationControlledServerAdapter, self._active_adapter(ServerCapability.STATION_CONTROL))
                adapter.set_station_active(unit_id, active)
                if active:
                    previous, self._write_origin = self._write_origin, "reset"
                    try:
                        for target, value in tuple(self._pending_point_resets.items()):
                            if self._target_active(target):
                                self._write_current(target, value)
                                del self._pending_point_resets[target]
                    finally:
                        self._write_origin = previous
                if self._scenario is not None:
                    self._scenario.station_changed(unit_id, active)
                    self._publish(self._scenario.snapshot())
                action = "启动" if active else "停止"
                self._publish(ServerLifecycle(LifecycleAction.STARTED if active else LifecycleAction.STOPPED,
                                              f"{action}从站 · Unit {unit_id}"))
            except ValueError as error:
                self._update(self._state, str(error))
