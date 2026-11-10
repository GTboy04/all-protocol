import asyncio
from dataclasses import replace
from datetime import datetime

import pytest

from all_protocol.adapters.modbus_tcp.model import Area, ModbusTarget
from all_protocol.adapters.modbus_tcp.values import DataType
from all_protocol.application.contracts import (
    Command, JournalEntry, Point, ProtocolRegistration, VerificationOptions, VerificationStatus, WriteResult, WriteStatus,
)
from all_protocol.application.session import Session
from all_protocol.point_tables import PointDefinition


def test_engineering_inverse_is_exact_for_integer_and_rejects_zero_scale():
    target = ModbusTarget(1, Area.HR, 0, 1, DataType.UINT64)
    point = PointDefinition("p", "大整数", target, scale=.1, offset=.2)
    assert point.raw_from_engineering("1844674407370955161.7") == 2**64 - 1
    assert replace(point, scale=-.1).raw_from_engineering("-0.1") == 3
    with pytest.raises(ValueError, match="不是整数"):
        point.raw_from_engineering("0.299999999999999999999999999999999999999999")
    with pytest.raises(ValueError, match="倍率为 0"):
        replace(point, scale=0).raw_from_engineering("0.2")
    with pytest.raises(ValueError, match="可转换范围"):
        point.raw_from_engineering("1e999999999")


@pytest.mark.parametrize(("expected", "actual", "absolute", "relative", "matches"), (
    (0., .01, .011, 0., True),
    (100., 100.5, 0., .01, True),
    (100., 100.5, 0., .001, False),
    (2**64 - 1, 2**64 - 2, 1., 1., False),
    (True, 1, 0., 0., True),
    (True, False, 1., 1., False),
))
def test_verification_compares_floats_with_tolerance_and_integers_exactly(expected, actual, absolute, relative, matches):
    options = VerificationOptions(absolute_tolerance=absolute, relative_tolerance=relative)
    write = JournalEntry(1, datetime.now(), "写入", "点位", "write", 0, "设备已应答",
                         write=WriteResult(WriteStatus.ACKNOWLEDGED, (expected,), options=options))
    result = Session._compare(write, (Point("0", "点位", actual),), True, False)
    assert result.status == (VerificationStatus.MATCH if matches else VerificationStatus.MISMATCH)


@pytest.mark.parametrize("cancel_during", ("delay", "read", "disconnect"))
def test_cancel_or_disconnect_only_ends_verification_and_preserves_ack(cancel_during):
    async def run():
        writes, entries = [], []
        acknowledged, read_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        target = ModbusTarget(1, Area.HR, 0, 1)

        class Client:
            async def connect(self):
                pass

            async def close(self):
                pass

            def function(self, command):
                return command.operation

            async def write(self, target, values):
                writes.append(values)

            async def read(self, target):
                read_started.set()
                await release.wait()
                return (Point("0", target.label, 42),)

        def emit(entry):
            if isinstance(entry, JournalEntry):
                entries.append(entry)
                if entry.write is not None:
                    acknowledged.set()

        session = Session(emit)
        await session.connect(ProtocolRegistration("modbus-tcp", "Modbus", lambda *_: Client(), True), None, "test")
        try:
            delay = 0 if cancel_during == "read" else 5000
            task = asyncio.create_task(session.execute(Command(target, (42,), VerificationOptions(delay))))
            await asyncio.wait_for(acknowledged.wait(), 1)
            if cancel_during == "disconnect":
                await asyncio.wait_for(session.disconnect(), 1)
            else:
                if cancel_during == "read":
                    await asyncio.wait_for(read_started.wait(), 1)
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert read_started.is_set() == (cancel_during == "read")
            release.set()
            if cancel_during != "disconnect":
                await asyncio.wait_for(session.execute(Command(target)), 1)
            write, = [entry for entry in entries if entry.write is not None]
            verification, = [entry for entry in entries if entry.verification is not None]
            assert writes == [(42,)] and write.write.status == WriteStatus.ACKNOWLEDGED
            assert verification.verification.status == VerificationStatus.UNAVAILABLE
            assert verification.verification.write_request_id == write.request_id
        finally:
            await session.disconnect()

    asyncio.run(run())
