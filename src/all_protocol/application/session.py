import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import count
from math import isclose
from time import monotonic
from typing import cast
from uuid import uuid4

from .contracts import (
    Client, ClientError, ClientEvents, Command, ConnectionState, ErrorKind,
    JournalEntry, OperationRejected, Point, ProtocolRegistration, ReadUpdate, SessionState, TableReadUpdate,
    VerificationOptions, VerificationResult, VerificationStatus, WritableClient, WriteResult, WriteStatus, SubscriptionHealth,
)
from .scheduling import RequestScheduler, Work, table_work
from .recovery import RecoveryPolicy


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
        self._subscriptions = {}
        self._subscription_specs = {}
        self._resume_subscriptions = None
        self._config = None
        self._registration = None
        self._recovery = RecoveryPolicy()
        self._recovery_task = None
        self._want_connection = False
        self._resume_polling = {}
        self._last_failure = None

    def _update(self, **changes):
        self._state = replace(self._state, **changes)
        self._emit(self._state)

    def _entry(self, request_id, operation, target, start, outcome, *,
               function="", error=None, detail="", packets=(), operation_id=None, generation=None,
               write=None, verification=None, value_metadata=()):
        entry = JournalEntry(request_id, datetime.now(), operation, target, function,
                             round((monotonic() - start) * 1000), outcome, error, detail, packets,
                             protocol=self._protocol, endpoint=self._state.endpoint, operation_id=operation_id,
                             session_id=self._state.session_id,
                             generation=self._generation if generation is None else generation,
                             write=write, verification=verification, value_metadata=value_metadata)
        self._emit(entry)
        return entry

    async def connect(self, protocol: ProtocolRegistration, config: object, endpoint: str, recovery=None):
        if (self._state.connection != ConnectionState.OFFLINE
                or self._recovery_task is not None and not self._recovery_task.done()):
            return
        self._recovery = recovery or RecoveryPolicy()
        self._registration = protocol
        self._want_connection = True
        self._update(recovery_attempt=0, recovery_limit=self._recovery.max_attempts,
                     recovery_error="", next_retry_at=None)
        await self._connect_once(protocol, config, endpoint)
        if self._online():
            await self._restore_read_tasks()
        else:
            self._schedule_recovery(endpoint)

    async def _connect_once(self, protocol, config, endpoint):
        self._generation += 1
        self._closed.clear()
        generation = self._generation
        self._connection_lost = False
        self._protocol = protocol.key
        self._config = config
        self._writable = protocol.writable
        self._last_failure = None
        self._update(connection=ConnectionState.CONNECTING, endpoint=endpoint, generation=generation,
                     session_id=str(uuid4()), next_retry_at=None)
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
            self._last_failure = failure
            await self._close_client()
            self._entry(request_id, "连接", endpoint, start, failure.kind,
                        error=failure.kind, detail=self._detail(failure), packets=tuple(packets))
            retry_decision_pending = self._want_connection and self._recovery.enabled
            self._update(connection=ConnectionState.CONNECTING if retry_decision_pending else ConnectionState.OFFLINE,
                         endpoint=endpoint if retry_decision_pending else "", recovery_error=self._detail(failure))
            self._closed.set()
        else:
            self._entry(request_id, "连接", endpoint, start, "已连接", packets=tuple(packets))
            self._update(connection=ConnectionState.ONLINE, recovery_error="",
                         last_communication_at=datetime.now())
        finally:
            self._packets = None
            self._task = None

    def _schedule_recovery(self, endpoint):
        if not self._want_connection or not self._recovery.enabled:
            return
        failure = self._last_failure
        if failure and (failure.kind not in (ErrorKind.CONNECT, ErrorKind.TIMEOUT, ErrorKind.DISCONNECTED)
                        or failure.code is not None):
            self._update(connection=ConnectionState.OFFLINE, endpoint="", next_retry_at=None,
                         recovery_error=f"恢复停止，请检查配置 · {self._detail(failure)}")
            return
        if self._recovery_task is None or self._recovery_task.done():
            self._update(connection=ConnectionState.WAITING, endpoint=endpoint, recovery_attempt=1,
                         next_retry_at=datetime.now() + timedelta(seconds=self._recovery.delay(1)))
            self._recovery_task = asyncio.create_task(self._recover(endpoint))

    async def _recover(self, endpoint):
        try:
            for attempt in range(1, self._recovery.max_attempts + 1):
                delay = self._recovery.delay(attempt)
                self._update(connection=ConnectionState.WAITING, endpoint=endpoint,
                             recovery_attempt=attempt, next_retry_at=datetime.now() + timedelta(seconds=delay))
                await asyncio.sleep(delay)
                if not self._want_connection:
                    return
                await self._connect_once(self._registration, self._config, endpoint)
                if self._online():
                    await self._restore_read_tasks()
                    if self._online():
                        return
                failure = self._last_failure
                if failure and (failure.kind not in (ErrorKind.CONNECT, ErrorKind.TIMEOUT, ErrorKind.DISCONNECTED)
                                or failure.code is not None):
                    self._update(connection=ConnectionState.OFFLINE, endpoint="", next_retry_at=None,
                                 recovery_error=f"恢复停止，请检查配置 · {self._detail(failure)}")
                    return
            self._update(connection=ConnectionState.OFFLINE, endpoint="", next_retry_at=None,
                         recovery_error=f"恢复失败，已达 {self._recovery.max_attempts} 次上限 · {self._state.recovery_error}")
        finally:
            self._recovery_task = None

    async def _restore_read_tasks(self):
        generation = self._generation
        key = self._protocol, self._state.endpoint, self._config
        if not self._recovery.restore_reads or self._resume_subscriptions is None or self._resume_subscriptions[0] != key:
            self._resume_polling.clear()
            self._resume_subscriptions = None
            if self._state.recovering_tables:
                self._update(recovering_tables=())
            return
        for table_id in tuple(self._resume_polling):
            if generation != self._generation or not self._online():
                return
            items, interval, stop_on_error = self._resume_polling.pop(table_id)
            self._start_group(table_id, items, interval, stop_on_error=stop_on_error)
        specs = self._resume_subscriptions[1]
        for table_id in tuple(specs):
            if generation != self._generation or not self._online():
                return
            spec = specs.pop(table_id, None)
            if spec is not None:
                await self.start_table_subscription(table_id, *spec)
        self._resume_subscriptions = None
        self._update(recovering_tables=())

    def _packet(self, sending: bool, data: bytes):
        if self._packets is not None:
            self._packets.append((sending, bytes(data)))

    def _lost(self, generation: int):
        if generation == self._generation and self._state.connection == ConnectionState.ONLINE and not self._connection_lost:
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
        if not unexpected:
            self._want_connection = False
            self._resume_subscriptions = None
            self._resume_polling.clear()
            task = self._recovery_task
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if self._recovery_task is task:
                    self._recovery_task = None
        if self._state.connection == ConnectionState.CLOSING:
            await self._closed.wait()
            return
        if self._state.connection == ConnectionState.OFFLINE:
            self._update(next_retry_at=None, recovering_tables=(),
                         **({"recovery_error": "", "recovery_attempt": 0} if not unexpected else {}))
            return
        start = monotonic()
        self._closed.clear()
        endpoint = self._state.endpoint
        if unexpected and self._recovery.restore_reads:
            self._resume_subscriptions = ((self._protocol, endpoint, self._config), dict(self._subscription_specs))
            self._resume_polling = {key: (group.items, round(group.interval * 1000), group.stop_on_error)
                                    for key, group in self._scheduler.groups.items()}
        else:
            self._resume_subscriptions = None
            self._resume_polling.clear()
        self._subscription_specs.clear()
        self._generation += 1
        self._update(connection=ConnectionState.CLOSING, polling=False, polling_groups=(), subscription_groups=(), generation=self._generation)
        self._subscriptions.clear()
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
                    detail="远端关闭了连接或链路已丢失" if unexpected else "")
        self._update(connection=ConnectionState.OFFLINE, endpoint="", busy=False, queued=0, manual_tables=(),
                     next_retry_at=None, recovering_tables=tuple(set(self._resume_polling) |
                         (set(self._resume_subscriptions[1]) if self._resume_subscriptions else set())),
                     **({"recovery_error": "", "recovery_attempt": 0} if not unexpected else {}))
        self._closed.set()
        if unexpected:
            self._last_failure = None
            self._schedule_recovery(endpoint)

    async def stop_polling(self):
        self._resume_polling.pop("__quick__", None)
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
        await self.stop_table_subscription(table_id)
        if not table_id or table_id == "__quick__":
            self._reject("轮询", "无效的轮询组标识，轮询未启动", table_id)
            return False
        if any(command.values is not None for _, command in commands):
            raise ClientError(ErrorKind.INPUT, "轮询不允许写请求")
        return self._start_group(table_id, table_work(table_id, commands), interval_ms) is not None

    async def stop_table_polling(self, table_id):
        self._resume_polling.pop(table_id, None)
        self._scheduler.stop_group(table_id)
        self._finish_operations()
        self._sync_schedule()
        self._wake.set()

    async def stop_all_polling(self):
        self._resume_polling.clear()
        if self._resume_subscriptions is not None:
            self._resume_subscriptions[1].clear()
        for table_id in tuple(self._subscriptions):
            await self.stop_table_subscription(table_id)
        for key in tuple(self._scheduler.groups):
            self._scheduler.stop_group(key)
        self._finish_operations()
        self._sync_schedule()
        self._wake.set()

    def _online(self):
        return self._client is not None and not self._connection_lost and self._state.connection == ConnectionState.ONLINE

    async def start_table_subscription(self, table_id, commands, interval_ms, queue_size=10):
        commands = tuple(commands)
        if (not self._online() or not table_id or table_id == "__quick__"
                or type(interval_ms) is not int or not 200 <= interval_ms <= 30000
                or type(queue_size) is not int or not 1 <= queue_size <= 1000
                or not commands or len(commands) > 1000
                or len(self._subscriptions) >= 64 and table_id not in self._subscriptions
                or any(command.values is not None for _, command in commands)):
            return self._reject("订阅", "订阅要求已连接、非空标量点表、200–30000 ms 周期和 1–1000 队列", table_id)
        subscribe = getattr(self._client, "subscribe", None)
        if subscribe is None:
            return self._reject("订阅", "当前协议不支持数据变更订阅", table_id)
        await self.stop_table_subscription(table_id)
        await self.stop_table_polling(table_id)
        generation = self._generation
        token = object()
        self._subscriptions[table_id] = (token, None)
        self._subscription_specs[table_id] = (commands, interval_ms, queue_size)

        def notify(index, point, detail):
            if (generation != self._generation or not self._online()
                    or self._subscriptions.get(table_id, (None,))[0] is not token):
                return
            point_id, command = commands[index]
            metadata = (point.metadata,) if point is not None and point.metadata is not None else ()
            native = "\n".join(item.summary for item in metadata)
            entry = self._entry(next(self._ids), "读取", command.target.label, monotonic(),
                                "数据变更" if point is not None else "订阅异常", function="DataChangeNotification",
                                error=None if point is not None else ErrorKind.RESPONSE,
                                detail="\n".join(filter(None, (detail, native))), value_metadata=metadata)
            self._emit(TableReadUpdate(table_id, point_id, ReadUpdate(command, entry, (point,) if point else None, generation)))

        try:
            subscription = await subscribe(tuple(command.target for _, command in commands), interval_ms, queue_size, notify)
        except BaseException as error:
            current = generation == self._generation and self._subscriptions.get(table_id, (None,))[0] is token
            if current:
                self._subscriptions.pop(table_id, None)
                self._subscription_specs.pop(table_id, None)
                self._update(subscription_groups=tuple(self._subscriptions))
            if isinstance(error, asyncio.CancelledError):
                raise
            if not current:
                return False
            self._reject("订阅", str(self._failure(error)), table_id)
            return False
        if generation != self._generation or self._subscriptions.get(table_id, (None,))[0] is not token:
            await subscription.close()
            return False
        self._subscriptions[table_id] = (token, subscription)
        self._update(subscription_groups=tuple(self._subscriptions))
        def health(alive, published, notified, silence, detail):
            if generation == self._generation and self._subscriptions.get(table_id, (None,))[0] is token and self._online():
                if alive and published is not None and (self._state.last_communication_at is None or published > self._state.last_communication_at):
                    self._update(last_communication_at=published)
                self._emit(SubscriptionHealth(table_id, generation, alive, published, notified, silence, detail))
        bind_health = getattr(subscription, "set_health_callback", None)
        if bind_health is not None:
            bind_health(health)
        return True

    async def stop_table_subscription(self, table_id):
        if self._resume_subscriptions is not None:
            self._resume_subscriptions[1].pop(table_id, None)
        previous = self._subscriptions.pop(table_id, None)
        self._subscription_specs.pop(table_id, None)
        self._update(subscription_groups=tuple(self._subscriptions))
        self._emit(SubscriptionHealth(table_id, self._generation, False, None, None, 0, "订阅已停止"))
        self._sync_schedule()
        if previous is not None and previous[1] is not None:
            await previous[1].close()

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
                     recovering_tables=tuple(set(self._resume_polling) |
                         (set(self._resume_subscriptions[1]) if self._resume_subscriptions else set())),
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
        if compare and all(point.metadata is None or point.metadata.quality == "Good" for point in points):
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
            failure = ClientError(ErrorKind.DISCONNECTED, "连接断开或请求已取消",
                                  write_sent=getattr(client, "write_sent", None) if command.values is not None else None)
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
            elif (failure.write_sent is True or failure.write_sent is None
                  and failure.kind != ErrorKind.INPUT and any(sending for sending, _ in packets)):
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
        metadata = tuple(point.metadata for point in (points or ()) if point.metadata is not None)
        if metadata:
            detail = "\n".join(item.summary for item in metadata)
            if any(item.quality != "Good" for item in metadata) and verify_write is None:
                outcome = "数据质量异常"
        if failure is None and generation == self._generation:
            self._update(last_communication_at=datetime.now())
        entry = self._entry(request_id, command.operation, command.target.label, start, outcome,
                            function=function, error=failure.kind if failure else None,
                            detail=detail, packets=tuple(packets), operation_id=operation_id,
                            generation=generation, write=write, verification=verification, value_metadata=metadata)
        if failure is not None and failure.kind == ErrorKind.DISCONNECTED and not canceled:
            self._lost(generation)
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
