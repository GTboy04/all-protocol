from dataclasses import dataclass
from datetime import datetime
from math import ceil

from .contracts import ErrorKind, JournalEntry
from .server_contracts import RequestMetadata, ServerEvent


@dataclass(frozen=True, slots=True)
class DiagnosticRow:
    time: datetime
    protocol: str
    device: tuple[str, ...]
    source: str
    operation: str
    target: str
    function: str
    outcome: str
    error: ErrorKind | None
    elapsed_ms: int | None
    identity: str
    detail: str
    packets: tuple[tuple[str, bool, bytes], ...]

    @classmethod
    def from_client(cls, entry: JournalEntry, device_name: str = ""):
        device = (entry.device_id,) if entry.device_id else (entry.protocol, entry.endpoint)
        identity = f"会话 {entry.session_id} / 请求 #{entry.request_id}" if entry.session_id else f"请求 #{entry.request_id}"
        if entry.protocol == "s7":
            # S7 may capture one PDU as multiple consecutive COTP frames.
            packets = []
            for sending, data in entry.packets:
                label = "TX" if sending else "RX"
                if packets and packets[-1][0] == label:
                    packets[-1] = (label, sending, packets[-1][2] + data)
                else:
                    packets.append((label, sending, data))
        else:
            packets = [("TX" if sending else "RX", sending, data) for sending, data in entry.packets]
        source = device_name or entry.device_id or " ".join(filter(None, (entry.protocol, entry.endpoint)))
        return cls(entry.time, entry.protocol, device, source or "—", entry.operation, entry.target, entry.function,
                   entry.outcome, entry.error, entry.elapsed_ms, identity, entry.detail, tuple(packets))

    @classmethod
    def from_server(cls, event: ServerEvent):
        payload = event.payload
        if not isinstance(payload, RequestMetadata):
            return cls(event.time, event.protocol, ("",), "—", "监听", event.endpoint, "", payload.result,
                       None, None, "", "", ())
        details = dict(payload.details)
        code = details.get("function")
        target = details.get("target") or ""
        if event.protocol == "s7":
            operation = {4: "读取", 5: "写入", 0xF0: "连接"}.get(code, "连接" if target == "COTP" else "其他")
        else:
            operation = ("读取" if code in (1, 2, 3, 4) else
                         "写入" if code in (5, 6, 15, 16) else "其他")
            if details.get("unit_id") is not None:
                target = f"Unit {details['unit_id']}"
                if details.get("offset") is not None:
                    target += f" · 地址 {details['offset']}"
                if details.get("count") is not None:
                    target += f" × {details['count']}"
        error = payload.error
        if error is None and payload.elapsed_ms is None:
            if not payload.tx:
                error = ErrorKind.RESPONSE
            elif operation != "连接" and payload.result != "成功":
                error = ErrorKind.PROTOCOL
        packets = tuple((label, request, data) for label, request, data in
                        (("RX", True, payload.rx), ("TX", False, payload.tx)) if data)
        identity = f"监听会话 {payload.session_id} / 请求 #{payload.request_id}" if payload.session_id else ""
        return cls(event.time, event.protocol, (payload.peer,), payload.peer or "—", operation,
                   target, f"0x{code:02X}" if type(code) is int else "", payload.result,
                   error, payload.elapsed_ms, identity, event.endpoint, packets)


@dataclass(frozen=True, slots=True)
class DiagnosticFilter:
    device: tuple[str, ...] | None = None
    since: float | None = None
    until: float | None = None
    operation: str = ""
    error: ErrorKind | bool | None = None

    def matches(self, row: DiagnosticRow) -> bool:
        stamp = row.time.timestamp()
        matches_error = (row.error is not None) == self.error if isinstance(self.error, bool) else (
            self.error is None or row.error == self.error
        )
        return ((self.device is None or row.device == self.device)
                and (self.since is None or stamp >= self.since)
                and (self.until is None or stamp <= self.until)
                and (not self.operation or row.operation == self.operation)
                and matches_error)


@dataclass(frozen=True, slots=True)
class RequestStats:
    count: int
    failed: int
    unknown_writes: int
    durations: int
    average_ms: float | None
    p50_ms: int | None
    p95_ms: int | None


def request_stats(rows) -> RequestStats:
    requests = [row for row in rows if row.operation in ("读取", "写入")
                and row.error != ErrorKind.INPUT and row.outcome != "已取消（未发送）"]
    times = sorted(row.elapsed_ms for row in requests if row.elapsed_ms is not None)
    return RequestStats(len(requests), sum(row.error is not None for row in requests),
                        sum(row.outcome == "写入结果未知" for row in requests), len(times),
                        sum(times) / len(times) if times else None,
                        times[ceil(.50 * len(times)) - 1] if times else None,
                        times[ceil(.95 * len(times)) - 1] if times else None)
