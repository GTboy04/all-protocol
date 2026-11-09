import asyncio
import socket
from dataclasses import replace

import pytest

from all_protocol.adapters.modbus_tcp.client import ModbusTcpClient
from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget, ModbusTcpConfig
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.modbus_tcp.values import DataType
from all_protocol.adapters.s7.client import S7Client
from all_protocol.adapters.s7.model import Area as S7Area, DataType as S7Type, S7Config, S7Target
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.application.contracts import ClientEvents
from all_protocol.application.server_contracts import ListenerState, ListenerUpdate
from all_protocol.application.server_session import ServerSession
from all_protocol.application.simulation import Assignment, RuleKind, SimulationCommand, SimulationRule, SimulationUpdate
from all_protocol.project import Project, ProjectFile, ProjectWorkspace
from all_protocol.server_protocols import SERVER_PROTOCOLS
from all_protocol.storage import S7ServerStorage, ServerStorage


RULES = (
    SimulationRule(RuleKind.FIXED, value=1),
    SimulationRule(RuleKind.TIMELINE, actions=(Assignment(150, 1),)),
    SimulationRule(RuleKind.RAMP, start_ms=200, period_ms=1000, high=1200),
)


def modbus_plan(port=1502):
    return ServerPlan(port=port, stations=(Station(1, points=(
        ServerPoint(Area.COIL, 0, 0, "启动位", simulation=RULES[0]),
        ServerPoint(Area.COIL, 1, 0, "反馈", simulation=RULES[1]),
        ServerPoint(Area.HR, 0, 0, "转速", DataType.UINT32, simulation=RULES[2]),
    )),))


def s7_plan(port=1102):
    return S7ServerPlan(port=port, points=(
        S7ServerPoint(S7Target(S7Area.DB, 1, 0, S7Type.BOOL, bit=0), 0, "启动位", RULES[0]),
        S7ServerPoint(S7Target(S7Area.DB, 1, 0, S7Type.BOOL, bit=1), 0, "反馈", RULES[1]),
        S7ServerPoint(S7Target(S7Area.DB, 1, 1, S7Type.WORD), 0, "转速", RULES[2]),
    ))


def test_rules_round_trip_through_local_plans_and_project(tmp_path):
    modbus, s7 = modbus_plan(), s7_plan()
    for storage, plan in ((ServerStorage(tmp_path), modbus), (S7ServerStorage(tmp_path), s7)):
        storage.save_plan(plan)
        assert storage.load_plan() == plan
    project = ProjectWorkspace.from_local("测试", tmp_path).project
    assert project == Project("测试", modbus=modbus, s7=s7)
    path = tmp_path / "rules.allprotocol.json"
    ProjectFile().save(project, path)
    assert ProjectFile().open(path) == project


@pytest.mark.parametrize("index", (0, 1), ids=("modbus", "s7"))
def test_clients_read_start_feedback_ramp_and_takeover(index):
    async def run():
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        events = []
        now = 100.0
        session = ServerSession(events.append, SERVER_PROTOCOLS[index].registration, clock=lambda: now)
        client_events = ClientEvents(lambda *_: None, lambda: None)
        if index == 0:
            plan = modbus_plan(port)
            client = ModbusTcpClient(ModbusTcpConfig(plan.host, port), client_events)
            start = ModbusTarget(1, Area.COIL, 0, 1)
            feedback = ModbusTarget(1, Area.COIL, 1, 1)
            speed = ModbusTarget(1, Area.HR, 0, 1, DataType.UINT32)
            partial_speed = ModbusTarget(1, Area.HR, 1, 1)
        else:
            plan = s7_plan(port)
            client = S7Client(S7Config(plan.host, port), client_events)
            start, feedback, speed = (point.target for point in plan.points)
            partial_speed = S7Target(S7Area.DB, 1, 2, S7Type.BYTE)
        start_key, feedback_key, speed_key = (point.target for point in plan.simulation_points())

        async def read(target):
            return (await client.read(target))[0].value

        async def wait_current(target, expected):
            async with asyncio.timeout(2):
                while await session.read_current(target) != expected:
                    await asyncio.sleep(.005)

        def held():
            return next(event.payload.held for event in reversed(events) if isinstance(event.payload, SimulationUpdate))

        await session.start(plan)
        try:
            await client.connect()
            assert await read(start) == 0
            await session.control_simulation(SimulationCommand.START)
            assert (await read(start), await read(feedback), await read(speed)) == (1, 0, 0)
            # Even writing the current value takes over the point. S7 feedback shares the same byte.
            await client.write(start, (1,))
            assert held() == {start_key}
            now = 100.150
            await wait_current(feedback_key, 1)
            assert (await read(feedback), await read(speed)) == (1, 0)
            now = 100.700
            await wait_current(speed_key, 600)
            assert await read(speed) == 600
            await client.write(partial_speed, (42,))
            assert held() == {start_key, speed_key}
            manual_speed = await read(speed)
            await session.edit_current(feedback_key, 0)
            assert held() == {start_key, feedback_key, speed_key}
            now = 101.200
            await session.control_simulation(SimulationCommand.RELEASE, start_key)
            assert (await read(feedback), await read(speed)) == (0, manual_speed)
            await session.control_simulation(SimulationCommand.RELEASE, speed_key)
            assert await read(speed) == 1200
            await session.control_simulation(SimulationCommand.PAUSE)
            assert session._simulation_task is None
            await session.control_simulation(SimulationCommand.RESET)
            assert not held() and (await read(start), await read(feedback), await read(speed)) == (0, 0, 0)
            await session.control_simulation(SimulationCommand.START)
            task = session._simulation_task
            await session.stop()
            assert task.done() and events[-1].payload == ListenerUpdate(ListenerState.STOPPED)
            await session.start(plan)
            assert not held() and await session.read_current(start_key) == 0
        finally:
            await client.close()
            await session.stop()

    asyncio.run(run())


def test_bad_rule_prevents_listener_start():
    async def run():
        plan = modbus_plan()
        station = plan.stations[0]
        invalid = replace(station.points[-1], simulation=SimulationRule(RuleKind.FIXED, value=-1))
        events = []
        session = ServerSession(events.append, SERVER_PROTOCOLS[0].registration)
        await session.start(replace(plan, stations=(replace(station, points=station.points[:-1] + (invalid,)),)))
        assert events[-1].payload.state == ListenerState.STOPPED
        assert "转速" in events[-1].payload.error
        assert session._simulation_task is None

    asyncio.run(run())
