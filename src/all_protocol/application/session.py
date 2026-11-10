import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from itertools import count
from math import isclose
from time import monotonic
from typing import cast
from uuid import uuid4

from .contracts import (
    Client, ClientError, ClientEvents, Command, ConnectionState, ErrorKind,
    JournalEntry, OperationRejected, Point, ProtocolRegistration, ReadUpdate, SessionState, TableReadUpdate,
    VerificationOptions, VerificationResult, VerificationStatus, WritableClient, WriteResult, WriteStatus,
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
        self._verification_task: asyncio.Task | None = None
        self._verification_reading = False
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
               function="", error=None, detail="", packets=(), operation_id=None, generation=None,
               write=None, verification=None):
        entry = JournalEntry(request_id, datetime.now(), operation, target, function,
                             round((monotonic() - start) * 1000), outcome, error, detail, packets,
                             protocol=self._protocol, endpoint=self._state.endpoint, operation_id=operation_id,
                             session_id=self._state.session_id,
                             generation=self._generation if generation is None else generation,
                             write=write, verification=verification)
        self._emit(entry)
        return entry

    async def connect(self, protocol: ProtocolRegistration, config: object, endpoint: str):
        if self._state.connection != ConnectionState.OFFLINE:
            return
        self._generation += 1
        self._closed.clear()
        generation = self._generation
        self._connection_lost = False
        self._protocol = protocol.key
        self._writable = protocol.writable
        self._update(connection=ConnectionState.CONNECTING, endpoint=endpoint, generation=generation,
                     session_id=str(uuid4()))
        # Only the owned handshake task is cancelled by disconnect, not its caller.
        self._task = asyncio.create_task(self._connect(protocol, config, endpoint, generation))
        try:
            await self._task
        except asyncio.CancelledError:
            if self._state.connection == ConnectionState.CONNECTING:
                await self.disconnect()
            raise

    async def _connect(self, protocol, config, endpoint, generation):
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
            self._task = None
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

    async def verify_write(self, entry: JournalEntry):
        write = entry.write
        if (write is None or write.target is None
                or write.status not in (WriteStatus.ACKNOWLEDGED, WriteStatus.UNKNOWN)
                or (entry.protocol, entry.endpoint) != (self._protocol, self._state.endpoint)):
            self._reject("独立核对", "请选择当前连接的已应答或结果未知的写入")
            return False
        return await self._submit((Work(Command(write.target), verify_write=entry),))

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
        if items and items[0].verify_write is not None:
            operation_name = "独立核对"
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
                active = self._scheduler.active
                # An issued read must drain its reply before another request uses this connection.
                if (active is not None and active.owner is operation
                        and self._verification_task is not None and not self._verification_reading):
                    self._verification_task.cancel()
                self._record_cancelled(cancelled)
            self._waiters.pop(operation.id, None)
            self._sync_schedule()
            self._wake.set()
            raise

    def _record_cancelled(self, operation):
        for item in operation.items[operation.index:]:
            if item.verify_write is not None:
                self._verification_unavailable(item.verify_write, operation.id, True, "排队核对已取消")
                continue
            command = item.command
            write = (WriteResult(WriteStatus.NOT_SENT, command.values, options=command.verification,
                                 engineering_values=command.engineering_values) if command.values is not None else None)
            self._entry(next(self._ids), item.command.operation, item.command.target.label, monotonic(),
                        "已取消（未发送）", error=ErrorKind.DISCONNECTED, detail="连接断开或任务取消，排队请求未发送",
                        operation_id=operation.id, write=write)

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
                    work, operation_id = item.work, item.owner.id
                    if work.verify_write is not None:
                        successful = await self._verify(work.verify_write, operation_id, independent=True)
                    else:
                        context = (work.table_id, work.members) if work.table_id else None
                        entry = await self._request(self._client, work.command, read_context=context,
                                                    operation_id=operation_id)
                        successful = entry.error is None
                        if (successful and entry.write is not None and entry.write.options is not None
                                and entry.write.options.delay_ms is not None):
                            if self._online() and operation_id in self._scheduler.operations:
                                await self._verify(entry, operation_id)
                            else:
                                self._verification_unavailable(entry, operation_id, False, "连接断开或任务已取消")
                finally:
                    self._scheduler.complete(item, successful)
                    self._finish_operations()
                    self._sync_schedule()
        finally:
            self._task = None

    async def _verify(self, write: JournalEntry, operation_id: int, *, independent=False) -> bool:
        options = write.write.options or VerificationOptions()

        async def read_after_delay():
            if not independent and options.delay_ms:
                await asyncio.sleep(options.delay_ms / 1000)
            if not self._online():
                self._verification_unavailable(write, operation_id, independent, "回读前连接已断开")
                return None
            self._verification_reading = True
            return await self._request(self._client, Command(write.write.target), operation_id=operation_id,
                                       verify_write=write, independent=independent)

        self._verification_task = task = asyncio.create_task(read_after_delay())
        try:
            entry = await task
            return entry is not None and entry.verification.status == VerificationStatus.MATCH
        except asyncio.CancelledError:
            if not self._verification_reading:
                self._verification_unavailable(write, operation_id, independent, "回读发送前已取消")
            if asyncio.current_task().cancelling():
                raise
            return False
        finally:
            self._verification_task = None
            self._verification_reading = False

    def _verification_unavailable(self, write, operation_id, independent, reason):
        result = VerificationResult(VerificationStatus.UNAVAILABLE, write.session_id, write.request_id,
                                    write.write.values, independent=independent)
        self._entry(next(self._ids), "回读验证", write.target, monotonic(), result.status, detail=reason,
                    operation_id=operation_id, verification=result)

    @staticmethod
    def _compare(write, points, compare, independent):
        expected = write.write.values
        observed = tuple(point.value for point in points) if points is not None else ()
        mismatches = ()
        status = VerificationStatus.UNAVAILABLE
        if compare:
            options = write.write.options or VerificationOptions()

            def equal(wanted, actual):
                if type(wanted) is float:
                    return type(actual) in (float, int) and isclose(wanted, actual, rel_tol=options.relative_tolerance,
                                                                   abs_tol=options.absolute_tolerance)
                if type(wanted) in (bool, int):
                    return type(actual) in (bool, int) and wanted == actual
                return type(wanted) is type(actual) and wanted == actual

            mismatches = tuple(index for index, (wanted, actual) in enumerate(zip(expected, observed, strict=True))
                               if not equal(wanted, actual))
            status = VerificationStatus.MISMATCH if mismatches else VerificationStatus.MATCH
        return VerificationResult(status, write.session_id, write.request_id, expected, observed, mismatches, independent)

    async def _request(self, client: Client, command: Command, *, read_context=None, operation_id=None,
                       verify_write=None, independent=False) -> JournalEntry:
        start = monotonic()
        generation = self._generation
        request_id = next(self._ids)
        packets = self._packets = []
        function = ""
        points: tuple[Point, ...] | None = None
        failure = None
        canceled = False
        values = command.values
        try:
            function = client.function(command)
            if values is None:
                points = await client.read(command.target)
                if read_context is not None and len(points) != len(read_context[1]):
                    raise ClientError(ErrorKind.RESPONSE, "命名点位响应数量与请求不一致")
                if verify_write is not None and len(points) != len(verify_write.write.values):
                    raise ClientError(ErrorKind.RESPONSE, "回读值数量与写入数量不一致")
            elif self._writable:
                prepare = getattr(command.target, "prepare_write", None)
                if prepare is not None:
                    values = prepare(values)
                await cast(WritableClient, client).write(command.target, values)
            else:
                raise ClientError(ErrorKind.INPUT, "当前协议不支持写入")
        except asyncio.CancelledError:
            canceled = True
            failure = ClientError(ErrorKind.DISCONNECTED, "连接断开或请求已取消")
        except Exception as error:
            failure = self._failure(error)
        finally:
            self._packets = None
        write = verification = None
        detail = self._detail(failure) if failure else ""
        if values is not None:
            if failure is None:
                status = WriteStatus.ACKNOWLEDGED
            elif failure.write_rejected:
                status = WriteStatus.REJECTED
            elif failure.kind != ErrorKind.INPUT and any(sending for sending, _ in packets):
                status = WriteStatus.UNKNOWN
            else:
                status = WriteStatus.NOT_SENT
            target = command.target if status != WriteStatus.NOT_SENT else None
            write = WriteResult(status, values, target, command.verification, command.engineering_values,
                                failure.code if failure else None)
        if verify_write is not None:
            discarded = operation_id not in self._scheduler.operations
            verification = self._compare(verify_write, points, failure is None and not discarded, independent)
            outcome = verification.status
            if discarded and not detail:
                detail = "核对已取消"
        elif failure is None:
            outcome = "设备已应答" if points is None else f"成功 · {len(points)} 点"
        elif write is not None and write.status == WriteStatus.UNKNOWN:
            outcome = write.status
        else:
            outcome = failure.kind
        entry = self._entry(request_id, command.operation, command.target.label, start, outcome,
                            function=function, error=failure.kind if failure else None,
                            detail=detail, packets=tuple(packets), operation_id=operation_id,
                            generation=generation, write=write, verification=verification)
        if command.values is None and verify_write is None:
            if read_context is None:
                self._emit(ReadUpdate(command, entry, points if failure is None else None, generation))
            else:
                table_id, members = read_context
                for index, (point_id, member) in enumerate(members):
                    values = (points[index],) if failure is None else None
                    self._emit(TableReadUpdate(table_id, point_id, ReadUpdate(member, entry, values, generation)))
        if canceled:
            raise asyncio.CancelledError
        return entry

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
