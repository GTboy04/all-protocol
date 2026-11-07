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
    JournalEntry, OperationRejected, Point, ProtocolRegistration, ReadUpdate, SessionState, TableReadUpdate,
    WritableClient,
)
from .scheduling import RequestScheduler, Work, table_work


async def wait_for_work(wake, delay):
    try:
        if delay is None:
            await wake.wait()
        else:
            await asyncio.wait_for(wake.wait(), delay)
    except TimeoutError:
        pass


class Session:
    """Owns the connection and all its tasks on one asyncio event loop."""

    def __init__(self, emit: Callable, *, clock=monotonic, wait=wait_for_work, queue_capacity=32):
        self._emit = emit
        self._state = SessionState()
        self._ids = count(1)
        self._client: Client | None = None
        self._protocol = ""
        self._task: asyncio.Task | None = None
        self._scheduler = RequestScheduler(clock=clock, capacity=queue_capacity)
        self._wait = wait
        self._wake = asyncio.Event()
        self._waiters = {}
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
               function="", error=None, detail="", packets=(), operation_id=None):
        entry = JournalEntry(request_id, datetime.now(), operation, target, function,
                             round((monotonic() - start) * 1000), outcome, error, detail, packets,
                             protocol=self._protocol, endpoint=self._state.endpoint, operation_id=operation_id)
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
        self._update(connection=ConnectionState.CONNECTING, endpoint=endpoint, generation=generation)
        start = monotonic()
        request_id = next(self._ids)
        packets = self._packets = []
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
                        error=failure.kind, detail=self._detail(failure), packets=tuple(packets))
            self._update(connection=ConnectionState.OFFLINE, endpoint="")
            self._closed.set()
        else:
            self._entry(request_id, "连接", endpoint, start, "已连接", packets=tuple(packets))
            self._update(connection=ConnectionState.ONLINE)
        finally:
            self._packets = None
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
        self._update(connection=ConnectionState.CLOSING, polling=False, polling_groups=(), generation=self._generation)
        operations, _groups = self._scheduler.clear()
        for waiter in self._waiters.values():
            waiter.cancel()
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for operation in operations:
            self._record_cancelled(operation)
        self._waiters.clear()
        # Let cancelled submitters unwind before reporting shutdown complete.
        await asyncio.sleep(0)
        self._scheduler.finished.clear()
        await self._close_client()
        error = ErrorKind.DISCONNECTED if unexpected else None
        self._entry(next(self._ids), "断开", endpoint, start, error or "已断开", error=error,
                    detail="远端关闭了 TCP 连接或连接已丢失" if unexpected else "")
        self._update(connection=ConnectionState.OFFLINE, endpoint="", busy=False, queued=0, manual_tables=())
        self._closed.set()

    async def stop_polling(self):
        group = self._scheduler.stop_group("__quick__")
        waiter = self._waiters.get(group.id) if group else None
        self._finish_operations()
        self._sync_schedule()
        self._wake.set()
        if waiter is not None:
            await asyncio.shield(waiter)

    async def execute(self, command: Command, interval_ms: int | None = None):
        if interval_ms is None:
            return await self._submit((Work(command),))
        if command.values is not None or type(interval_ms) is not int or not 200 <= interval_ms <= 30000:
            raise ClientError(ErrorKind.INPUT, "轮询仅支持读取，且间隔为 200–30000 ms")
        group = self._start_group("__quick__", (Work(command),), interval_ms, stop_on_error=True)
        if group is None:
            return False
        waiter = self._waiters[group.id]
        try:
            return await waiter
        except asyncio.CancelledError:
            self._scheduler.stop_group(group.key)
            self._waiters.pop(group.id, None)
            self._sync_schedule()
            self._wake.set()
            raise

    async def execute_table(self, table_id: str, commands: tuple[tuple[str, Command], ...]):
        """Enqueue one table snapshot, interleaved fairly at request boundaries."""
        if any(command.values is not None for _, command in commands):
            raise ClientError(ErrorKind.INPUT, "整表读取不允许包含写请求")
        return await self._submit(table_work(table_id, commands))

    async def start_table_polling(self, table_id, commands, interval_ms):
        if not table_id or table_id == "__quick__":
            self._reject("轮询", "无效的轮询组标识，轮询未启动", table_id)
            return False
        if any(command.values is not None for _, command in commands):
            raise ClientError(ErrorKind.INPUT, "轮询不允许写请求")
        return self._start_group(table_id, table_work(table_id, commands), interval_ms) is not None

    async def stop_table_polling(self, table_id):
        self._scheduler.stop_group(table_id)
        self._finish_operations()
        self._sync_schedule()
        self._wake.set()

    async def stop_all_polling(self):
        for key in tuple(self._scheduler.groups):
            self._scheduler.stop_group(key)
        self._finish_operations()
        self._sync_schedule()
        self._wake.set()

    def _online(self):
        return self._client is not None and not self._connection_lost and self._state.connection == ConnectionState.ONLINE

    def _reject(self, operation, detail, table_id=""):
        self._emit(OperationRejected(operation, detail, self._generation, table_id))
        self._sync_schedule()
        return None

    def _start_group(self, key, items, interval_ms, **options):
        if not self._online():
            return self._reject("轮询", "当前未连接，轮询未启动", key)
        try:
            group = self._scheduler.start_group(key, items, interval_ms, **options)
        except ValueError as error:
            return self._reject("轮询", str(error), key)
        if key == "__quick__":
            self._waiters[group.id] = asyncio.get_running_loop().create_future()
        self._launch()
        return group

    async def _submit(self, items):
        table_id = items[0].table_id if items else ""
        operation_name = items[0].command.operation if items else "读取"
        if not self._online():
            self._reject(operation_name, "当前未连接，请求未发送", table_id)
            return False
        try:
            operation = self._scheduler.submit(items)
        except ValueError as error:
            self._reject(operation_name, str(error), table_id)
            return False
        waiter = asyncio.get_running_loop().create_future()
        self._waiters[operation.id] = waiter
        self._finish_operations()
        self._launch()
        try:
            return await waiter
        except asyncio.CancelledError:
            cancelled = self._scheduler.cancel(operation.id)
            if cancelled is not None:
                self._record_cancelled(cancelled)
            self._waiters.pop(operation.id, None)
            self._sync_schedule()
            self._wake.set()
            raise

    def _record_cancelled(self, operation):
        for item in operation.items[operation.index:]:
            self._entry(next(self._ids), item.command.operation, item.command.target.label, monotonic(),
                        "已取消（未发送）", error=ErrorKind.DISCONNECTED, detail="连接断开或任务取消，排队请求未发送",
                        operation_id=operation.id)

    def _sync_schedule(self):
        scheduler = self._scheduler
        self._update(busy=scheduler.active is not None or bool(scheduler.operations),
                     queued=len(scheduler.operations), queue_capacity=scheduler.capacity,
                     polling="__quick__" in scheduler.groups,
                     polling_groups=tuple(key for key in scheduler.groups if key != "__quick__"),
                     manual_tables=tuple(sorted({item.table_id for operation in scheduler.operations.values()
                                                 for item in operation.items if item.table_id})))

    def _finish_operations(self):
        while self._scheduler.finished:
            operation_id, successful = self._scheduler.finished.pop(0)
            waiter = self._waiters.pop(operation_id, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(successful)

    def _launch(self):
        self._sync_schedule()
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_schedule())

    async def _run_schedule(self):
        try:
            while self._online():
                self._wake.clear()
                item = self._scheduler.take()
                if item is None:
                    await self._wait(self._wake, self._scheduler.delay())
                    continue
                self._sync_schedule()
                successful = False
                try:
                    context = (item.work.table_id, item.work.members) if item.work.table_id else None
                    successful = await self._request(self._client, item.work.command, read_context=context,
                                                     operation_id=item.owner.id)
                finally:
                    self._scheduler.complete(item, successful)
                    self._finish_operations()
                    self._sync_schedule()
        finally:
            self._task = None

    async def _request(self, client: Client, command: Command, *, read_context=None, operation_id=None) -> bool:
        start = monotonic()
        generation = self._generation
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
                if read_context is not None and len(points) != len(read_context[1]):
                    raise ClientError(ErrorKind.RESPONSE, "命名点位响应数量与请求不一致")
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
                            detail=self._detail(failure) if failure else "", packets=tuple(packets), operation_id=operation_id)
        if command.values is None:
            if read_context is None:
                self._emit(ReadUpdate(command, entry, points if failure is None else None, generation))
            else:
                table_id, members = read_context
                for index, (point_id, member) in enumerate(members):
                    values = (points[index],) if failure is None else None
                    self._emit(TableReadUpdate(table_id, point_id, ReadUpdate(member, entry, values, generation)))
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
