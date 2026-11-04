import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from itertools import count
from time import monotonic
from typing import cast

from .contracts import (
    Client, ClientError, ClientEvents, Command, ConnectionState, ErrorKind,
    JournalEntry, Point, ProtocolRegistration, ReadUpdate, SessionState,
    WritableClient,
)


class Session:
    """Owns the connection and all its tasks on one asyncio event loop."""

    def __init__(self, emit: Callable[[SessionState | JournalEntry | ReadUpdate], None]):
        self._emit = emit
        self._state = SessionState()
        self._ids = count(1)
        self._client: Client | None = None
        self._protocol = ""
        self._task: asyncio.Task | None = None
        self._delay: asyncio.Event | None = None
        self._packets: list[tuple[bool, bytes]] | None = None
        self._writable = False
        self._generation = 0
        self._connection_lost = False
        self._closed = asyncio.Event()
        self._closed.set()

    def _update(self, **changes):
        self._state = replace(self._state, **changes)
        self._emit(self._state)

    def _entry(self, request_id, operation, target, start, outcome, *,
               function="", error=None, detail="", packets=()):
        entry = JournalEntry(request_id, datetime.now(), operation, target, function,
                             round((monotonic() - start) * 1000), outcome, error, detail, packets,
                             protocol=self._protocol, endpoint=self._state.endpoint)
        self._emit(entry)
        return entry

    async def connect(self, protocol: ProtocolRegistration, config: object, endpoint: str):
        if self._state.connection != ConnectionState.OFFLINE:
            return
        self._generation += 1
        self._closed.clear()
        generation = self._generation
        self._connection_lost = False
        self._task = asyncio.current_task()
        self._protocol = protocol.key
        self._writable = protocol.writable
        self._update(connection=ConnectionState.CONNECTING, endpoint=endpoint)
        start = monotonic()
        request_id = next(self._ids)
        try:
            events = ClientEvents(self._packet, lambda: self._lost(generation))
            self._client = protocol.create_client(config, events)
            await self._client.connect()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = self._failure(error)
            await self._close_client()
            self._entry(request_id, "连接", endpoint, start, failure.kind,
                        error=failure.kind, detail=self._detail(failure))
            self._update(connection=ConnectionState.OFFLINE, endpoint="")
            self._closed.set()
        else:
            self._entry(request_id, "连接", endpoint, start, "已连接")
            self._update(connection=ConnectionState.ONLINE)
        finally:
            self._task = None

    def _packet(self, sending: bool, data: bytes):
        if self._packets is not None:
            self._packets.append((sending, bytes(data)))

    def _lost(self, generation: int):
        if generation == self._generation and self._state.connection == ConnectionState.ONLINE:
            self._connection_lost = True
            asyncio.create_task(self.disconnect(unexpected=True))

    async def _close_client(self):
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                logging.exception("Could not close protocol client")
            self._client = None

    async def disconnect(self, *, unexpected: bool = False):
        if self._state.connection == ConnectionState.CLOSING:
            await self._closed.wait()
            return
        if self._state.connection == ConnectionState.OFFLINE:
            return
        start = monotonic()
        endpoint = self._state.endpoint
        self._generation += 1
        self._update(connection=ConnectionState.CLOSING, polling=False)
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._close_client()
        error = ErrorKind.DISCONNECTED if unexpected else None
        self._entry(next(self._ids), "断开", endpoint, start, error or "已断开", error=error,
                    detail="远端关闭了 TCP 连接或连接已丢失" if unexpected else "")
        self._update(connection=ConnectionState.OFFLINE, endpoint="", busy=False)
        self._closed.set()

    async def stop_polling(self):
        if self._delay is not None:
            self._delay.set()

    async def execute(self, command: Command, interval_ms: int | None = None):
        if self._client is None or self._task is not None or self._connection_lost:
            return
        if self._state.connection != ConnectionState.ONLINE:
            return
        if interval_ms is not None and (interval_ms < 200 or command.values is not None):
            raise ClientError(ErrorKind.INPUT, "轮询仅支持读取，且间隔至少为 200 ms")
        self._task = asyncio.current_task()
        self._delay = asyncio.Event() if interval_ms is not None else None
        self._update(busy=True, polling=self._delay is not None)
        try:
            while await self._request(self._client, command):
                if self._delay is None or self._delay.is_set():
                    break
                try:
                    await asyncio.wait_for(self._delay.wait(), interval_ms / 1000)
                    break
                except TimeoutError:
                    pass
        finally:
            self._task = None
            self._delay = None
            self._update(busy=False, polling=False)

    async def _request(self, client: Client, command: Command) -> bool:
        start = monotonic()
        request_id = next(self._ids)
        packets = self._packets = []
        function = ""
        points: tuple[Point, ...] | None = None
        failure = None
        canceled = False
        try:
            function = client.function(command)
            if command.values is None:
                points = await client.read(command.target)
            elif self._writable:
                await cast(WritableClient, client).write(command.target, command.values)
            else:
                raise ClientError(ErrorKind.INPUT, "当前协议不支持写入")
        except asyncio.CancelledError:
            canceled = True
            failure = ClientError(ErrorKind.DISCONNECTED, "连接已断开，请求中止")
        except Exception as error:
            failure = self._failure(error)
        finally:
            self._packets = None
        if failure is None:
            outcome = "设备已应答" if points is None else f"成功 · {len(points)} 点"
        elif command.values is not None and any(sending for sending, _ in packets) and (
            failure.kind != ErrorKind.INPUT and not failure.write_rejected
        ):
            outcome = "写入结果未知"
        else:
            outcome = failure.kind
        entry = self._entry(request_id, command.operation, command.target.label, start, outcome,
                            function=function, error=failure.kind if failure else None,
                            detail=self._detail(failure) if failure else "", packets=tuple(packets))
        if command.values is None:
            self._emit(ReadUpdate(command, entry, points))
        if canceled:
            raise asyncio.CancelledError
        return failure is None

    @staticmethod
    def _failure(error: Exception) -> ClientError:
        if isinstance(error, ClientError):
            return error
        logging.exception("Unexpected protocol failure")
        return ClientError(ErrorKind.INTERNAL, str(error))

    @staticmethod
    def _detail(error: ClientError) -> str:
        if error.code is not None:
            return f"{error}（异常码 0x{error.code:02X}）"
        return str(error)
