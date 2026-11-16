import asyncio
import socket
import struct
from functools import partial
from datetime import datetime
from threading import Event, get_ident
from time import monotonic, sleep

from PySide6.QtCore import QTimer

from all_protocol.application.server_contracts import RequestMetadata, ServerEvent, ServerLifecycle, LifecycleAction
from all_protocol.storage import ServerStorage
from all_protocol.ui.server_io_bridge import ServerIoBridge
from all_protocol.ui.main_window import Rows, _table


def request(number):
    return ServerEvent("modbus-tcp", "127.0.0.1:1502", datetime.now(),
                       RequestMetadata("client", "成功", b"rx", b"tx", (), request_id=number))


def wait_until(app, condition):
    deadline = monotonic() + 5
    while not condition() and monotonic() < deadline:
        app.processEvents()
        sleep(0.001)
    assert condition()


class Session:
    def __init__(self, emit):
        self.emit = emit

    async def stop(self):
        pass

    async def send(self, events):
        for event in events:
            self.emit(event)


def test_slow_disk_does_not_block_gui_or_protocol_and_shutdown_flushes(qt_application, tmp_path):
    entered, release, replied = Event(), Event(), Event()
    storage = ServerStorage(tmp_path)
    original = storage.append_events
    writer_threads = []

    def slow_append(events):
        writer_threads.append(get_ident())
        entered.set()
        assert release.wait(5)
        return original(events)

    storage.append_events = slow_append
    bridge = ServerIoBridge(Session, Session.stop, storage)
    batches = []
    bridge.events.connect(batches.append)
    ticks = []
    heartbeat = QTimer()
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: ticks.append(True))
    heartbeat.start()
    try:
        bridge.submit(bridge.session.send(tuple(request(i) for i in range(1000))))
        wait_until(qt_application, entered.is_set)

        async def protocol_reply():
            replied.set()

        bridge.submit(protocol_reply())
        wait_until(qt_application, lambda: replied.is_set() and len(ticks) >= 3)
        assert not batches  # the GUI keeps ticking while the journal is blocked
        release.set()
        bridge.join()
        bridge._deliver()
        recent = storage.recent_events()
        assert len(recent) == 500
        assert [event.payload.request_id for event in recent] == list(range(999, 499, -1))
        from all_protocol.database import connect
        with connect(storage.database_path) as connection:
            assert connection.execute("SELECT count(*) FROM journal_entries").fetchone()[0] == 1000
        assert all(thread != get_ident() for thread in writer_threads)
        assert sum(len(batch.events) for batch in batches) <= 500
        assert any(batch.clipped for batch in batches)
    finally:
        release.set()
        heartbeat.stop()
        bridge.join()


def test_overload_is_bounded_and_preserves_lifecycle(qt_application, tmp_path):
    entered, release = Event(), Event()
    storage = ServerStorage(tmp_path)
    original = storage.append_events

    def slow_append(events):
        entered.set()
        assert release.wait(5)
        return original(events)

    storage.append_events = slow_append
    bridge = ServerIoBridge(Session, Session.stop, storage)
    bridge.PENDING_REQUESTS = 20
    batches = []
    bridge.events.connect(batches.append)
    lifecycle = ServerEvent("modbus-tcp", "", datetime.now(),
                            ServerLifecycle(LifecycleAction.STOPPED, "停止"))
    try:
        bridge._emit(request(0))
        assert entered.wait(2)
        for i in range(1, 101):
            bridge._emit(request(i))
        bridge._emit(lifecycle)
        with bridge._condition:
            assert len(bridge._pending) == 21
        release.set()
        bridge.join()
        bridge._deliver()
        assert any("未保存" in batch.error for batch in batches)
        assert lifecycle in tuple(event for batch in batches for event in batch.events)
        assert storage.recent_events()[0] == lifecycle
    finally:
        release.set()
        bridge.join()


def test_batch_storage_keeps_rotation_boundary(tmp_path, monkeypatch):
    storage = ServerStorage(tmp_path)
    events = tuple(request(i) for i in range(6))
    monkeypatch.setattr("all_protocol.storage.LOG_LIMIT_BYTES", 650)
    rotations = storage.append_events(events)
    assert any(rotations)
    last = max(i for i, rotated in enumerate(rotations) if rotated)
    assert storage.recent_events() == tuple(reversed(events[last:]))


def test_batch_insert_preserves_selected_packet(qt_application):
    model = Rows(("请求",), lambda number: (str(number),))
    table = _table(model)
    model.prepend_many(range(499, -1, -1))
    table.selectRow(10)
    selected = table.currentIndex().data()
    inserted = []
    model.rowsInserted.connect(lambda *args: inserted.append(args))
    model.prepend_many((503, 502, 501, 500))
    assert len(inserted) == 1
    assert model.rowCount() == 500
    assert table.currentIndex().data() == selected
    table.deleteLater()


def test_real_modbus_polling_continues_with_blocked_journal(qt_application, tmp_path):
    from all_protocol.adapters.modbus_tcp.model import Area
    from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
    from all_protocol.application.server_session import ServerSession
    from all_protocol.server_protocols import SERVER_PROTOCOLS

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    storage = ServerStorage(tmp_path)
    entered, release, finished = Event(), Event(), Event()
    original = storage.append_events

    def slow_append(events):
        entered.set()
        assert release.wait(5)
        return original(events)

    storage.append_events = slow_append
    bridge = ServerIoBridge(partial(ServerSession, registration=SERVER_PROTOCOLS[0].registration),
                            ServerSession.stop, storage)
    replies = []
    errors = []

    async def poll():
        try:
            await bridge.session.start(ServerPlan(port=port, stations=(
                Station(1, points=(ServerPoint(Area.HR, 0, 42),)),)))
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                for i in range(1000):
                    writer.write(struct.pack(">HHHB", i, 0, 6, 1) + bytes.fromhex("03 0000 0001"))
                    await writer.drain()
                    replies.append(await asyncio.wait_for(reader.readexactly(11), 2))
            finally:
                writer.close()
                await writer.wait_closed()
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    try:
        bridge.submit(poll())
        wait_until(qt_application, lambda: finished.is_set() and entered.is_set())
        assert not errors
        assert len(replies) == 1000
        assert all(reply[-2:] == b"\x00\x2a" for reply in replies)
        assert not release.is_set()
    finally:
        release.set()
        bridge.join()
    from all_protocol.database import connect
    with connect(storage.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM journal_entries").fetchone()[0] == 1002
