import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from all_protocol.adapters.s7.wire import tpkt
from all_protocol.application.contracts import ErrorKind, JournalEntry
from all_protocol.application.diagnostics import DiagnosticFilter, DiagnosticRow, request_stats
from all_protocol.application.faults import FaultAction, FaultInjector, FaultOperation, FaultRule, FaultUpdate
from all_protocol.application.packet_decoder import decode_packet


def fields(tree):
    for item in tree:
        yield item
        yield from fields(item.children)


@pytest.mark.parametrize(("is_request", "raw", "expected"), (
    (True, "000100000006010300100002", {"事务 ID": (0, 2), "Unit ID": (6, 7), "起始地址": (8, 10)}),
    (False, "000100000003018303", {"功能码": (7, 8), "异常码": (8, 9)}),
    (False, "00010000000701030400010002", {"数据字节数": (8, 9), "数据": (9, 13)}),
    (True, "00010000000b0110001000020400010002", {"数量": (10, 12), "数据": (13, 17)}),
))
def test_modbus_offsets_and_every_truncated_prefix(is_request, raw, expected):
    packet = bytes.fromhex(raw)
    tree = {field.name: field for field in fields(decode_packet("modbus-tcp", packet, request=is_request))}
    assert "解析失败" not in tree
    for name, span in expected.items():
        assert tree[name].spans == (span,)
    for end in range(len(packet)):
        error = decode_packet("modbus-tcp", packet[:end], request=is_request)[-1]
        assert error.name == "解析失败" and f"偏移 {end} ·" in error.value


def test_s7_reassembly_uses_raw_offsets_across_tpkt_headers():
    pdu = bytes.fromhex("320100000001000e00000401120a10020002000184000010")
    packet = tpkt(b"\x02\xf0\x00" + pdu[:5]) + tpkt(b"\x02\xf0\x80" + pdu[5:])
    tree = {field.name: field for field in fields(decode_packet("s7", packet, request=True))}
    assert "解析失败" not in tree
    assert tree["PDU 引用号"].spans == ((11, 12), (19, 20))
    assert tree["位地址"].spans == ((35, 38),)
    for end in range(len(packet)):
        error = decode_packet("s7", packet[:end], request=True)[-1]
        assert error.name == "解析失败" and f"偏移 {end} ·" in error.value


def test_s7_setup_and_write_reply_fields_and_invalid_length():
    cotp = bytes.fromhex("0300001611e00000000100c0010ac1020100c2020101")
    tree = {field.name: field for field in fields(decode_packet("s7", cotp, request=True))}
    assert tree["目标 TSAP"].spans == ((20, 22),)
    ack = tpkt(bytes.fromhex("02f0803203000000010002000100000501ff"))
    tree = {field.name: field for field in fields(decode_packet("s7", ack, request=False))}
    assert tree["写入状态 1"].spans == ((21, 22),)
    error = decode_packet("s7", bytes.fromhex("0300000602f0"), request=True)[-1]
    assert error.spans == ((2, 3),)


def test_filtered_statistics_share_the_row_scope_and_preserve_unknown_writes():
    now = datetime.now()
    first = JournalEntry(1, now, "写入", "HR", "0x06", 80, "写入结果未知", ErrorKind.TIMEOUT,
                         device_id="a", session_id="session-a")
    rows = [DiagnosticRow.from_client(row) for row in (
        first, replace(first, operation="读取", error=None, elapsed_ms=20, outcome="成功"),
        replace(first, device_id="b"), replace(first, operation="连接"),
        replace(first, error=ErrorKind.INPUT, elapsed_ms=0, outcome="输入错误"),
        replace(first, error=ErrorKind.DISCONNECTED, elapsed_ms=0, outcome="已取消（未发送）"),
        replace(first, time=now - timedelta(seconds=90)),
    )]
    scoped = [row for row in rows if DiagnosticFilter(("a",), now.timestamp() - 60).matches(row)]
    stats = request_stats(scoped)
    assert (stats.count, stats.failed, stats.unknown_writes, stats.durations) == (2, 1, 1, 2)
    assert (stats.average_ms, stats.p50_ms, stats.p95_ms) == (50, 20, 80)
    assert rows[0].identity == "会话 session-a / 请求 #1"
    assert [row for row in scoped if DiagnosticFilter(operation="写入", error=ErrorKind.TIMEOUT).matches(row)] == rows[:1]
    assert [row for row in scoped if DiagnosticFilter(error=False).matches(row)] == rows[1:2]


def test_fault_claims_match_and_expire_and_reset_releases_existing_delay():
    async def run():
        now = 0.0
        events = []
        injector = FaultInjector(lambda: now, events.append)
        injector.configure(FaultRule(FaultAction.EXCEPTION, FaultOperation.WRITE, count=2))
        assert injector.claim(FaultOperation.READ) is None
        assert injector.claim(FaultOperation.WRITE) is not None
        assert injector.claim(FaultOperation.WRITE) is not None
        assert injector.claim(FaultOperation.WRITE) is None
        assert injector.update.triggered == 2 and injector.update.remaining == 0
        injector.configure(FaultRule(FaultAction.DELAY, count=None, duration_ms=1000, delay_ms=60000))
        hit = injector.claim(FaultOperation.READ)
        now = 1.0
        assert injector.claim(FaultOperation.READ) is None
        assert not injector.update.active and injector.update.triggered == 1
        waiting = asyncio.create_task(hit.wait())
        await asyncio.sleep(0)
        assert not waiting.done()
        injector.configure(None)
        await asyncio.wait_for(waiting, 1)
        assert events[-1] == FaultUpdate()

    asyncio.run(run())
