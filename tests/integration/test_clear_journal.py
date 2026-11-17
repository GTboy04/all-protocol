from dataclasses import replace
from datetime import datetime
from functools import partial
from threading import Event
from time import monotonic, sleep

import pytest

from all_protocol.application.contracts import JournalEntry, ProtocolRegistration
from all_protocol.application.server_contracts import RequestMetadata, ServerEvent
from all_protocol.application.server_session import ServerSession
from all_protocol.server_protocols import SERVER_PROTOCOLS
from all_protocol.storage import ConnectionProfile, LocalHistory, ServerStorage
from all_protocol.ui.io_bridge import IoBridge
from all_protocol.ui.main_window import DesktopProtocol, MainWindow
from all_protocol.ui.modbus_server_pane import ModbusServerPane
from all_protocol.ui.modbus_tcp_form import ModbusTcpForm
from all_protocol.ui.server_io_bridge import ServerIoBridge


def entry(number=1, **scope):
    return JournalEntry(number, datetime.now(), "读取", "HR / 0", "0x03", 1, "成功", **scope)


def request(number=1, endpoint="127.0.0.1:1502"):
    return ServerEvent("modbus-tcp", endpoint, datetime.now(), RequestMetadata("client", "成功", request_id=number))


def wait_until(app, predicate):
    deadline = monotonic() + 5
    while not predicate() and monotonic() < deadline:
        app.processEvents()
        sleep(.001)
    assert predicate()


def test_clear_persisted_logs_keeps_profiles_plans_and_other_streams(tmp_path):
    history, server = LocalHistory(tmp_path), ServerStorage(tmp_path)
    profile = ConnectionProfile("modbus-tcp", "127.0.0.1:502", {}, 1000)
    history.remember(profile)
    config = server.load_plan()
    server.save_plan(config)
    history.append_entry(entry())
    old_request = request()
    server.append_event(old_request)
    history.clear_log()
    assert LocalHistory(tmp_path).recent_entries() == ()
    assert history.profiles() == (profile,)
    assert server.recent_events() == (old_request,)
    server.clear_log()
    assert ServerStorage(tmp_path).recent_events() == ()
    assert server.load_plan() == config
    assert server.read_log().retention.rotated_at is not None
    newer = request(2)
    server.append_event(newer)
    assert ServerStorage(tmp_path).recent_events() == (newer,)
    history.append_entry(entry(2))
    assert len(LocalHistory(tmp_path).recent_entries()) == 1


def test_client_pause_keeps_history_live_and_clear_discards_pending(qt_application, tmp_path):
    history = LocalHistory(tmp_path)
    bridge = IoBridge()
    window = MainWindow(bridge, (DesktopProtocol(
        ProtocolRegistration("modbus-tcp", "Modbus", lambda *_: None), ModbusTcpForm),), history)
    try:
        old, newer = entry(), entry(2)
        window._append_journal(old)
        window.log_table.selectRow(0)
        detail = window.detail.toPlainText()
        window.journal_view.pause_button.click()
        window._append_journal(newer)
        assert window.journal.items == [old]
        assert window.detail.toPlainText() == detail
        assert history.recent_entries() == (newer, old)
        assert window.report_log().entries == (newer, old)
        window.journal_view.pause_button.click()
        assert window.journal.items == [newer, old]
        assert window.log_table.currentIndex().row() == 1
        window.journal_view.pause_button.click()
        window._append_journal(entry(3))
        window.journal_view.clear_button.click()
        window.journal_view.pause_button.click()
        assert window.journal.items == []
        assert window.detail.toPlainText() == ""
    finally:
        bridge.join()
        window.close()
        window.deleteLater()


def test_server_pause_keeps_batch_persistence_live(qt_application, tmp_path):
    storage = ServerStorage(tmp_path)
    bridge = ServerIoBridge(partial(ServerSession, registration=SERVER_PROTOCOLS[0].registration),
                            ServerSession.stop, storage)
    pane = ModbusServerPane(bridge, storage)
    try:
        bridge._emit(request(1))
        wait_until(qt_application, lambda: pane.requests.rowCount() == 1)
        pane.request_table.selectRow(0)
        detail = pane.detail.toPlainText()
        pane.journal_view.pause_button.click()
        bridge._emit(request(2))
        wait_until(qt_application, lambda: len(storage.recent_events()) == 2)
        bridge._deliver()
        assert pane.requests.rowCount() == 1
        assert pane.detail.toPlainText() == detail
        pane.journal_view.pause_button.click()
        assert [item.payload.request_id for item in pane.requests.items] == [2, 1]
        assert pane.request_table.currentIndex().row() == 1
    finally:
        bridge.join()
        qt_application.processEvents()
        pane.close()
        pane.deleteLater()


def test_clear_selected_device_and_endpoint_preserves_others(tmp_path):
    history = LocalHistory(tmp_path)
    base = entry(protocol="modbus-tcp", endpoint="host:502", device_id="a")
    other = replace(base, device_id="b")
    legacy = replace(base, device_id="")
    different = replace(legacy, endpoint="other:502")
    for record in (base, other, legacy, different):
        history.append_entry(record)
    history.clear_log(device_id="a", protocol="modbus-tcp", endpoint="host:502")
    assert history.recent_entries() == (different, other)
    server = ServerStorage(tmp_path)
    first, second = request(), request(2, "127.0.0.1:1503")
    server.append_events((first, second))
    server.clear_log(endpoint=first.endpoint)
    assert server.recent_events() == (second,)


def test_clear_before_legacy_migration_does_not_restore_old_entries(tmp_path):
    history = LocalHistory(tmp_path)
    history.journal_path.write_text(
        '{"request_id":1,"time":"2026-11-16T12:00:00","operation":"读取","target":"HR",'
        '"function":"03","elapsed_ms":1,"outcome":"成功"}\n', encoding="utf-8")
    history.clear_log()
    assert history.journal_path.exists()
    assert LocalHistory(tmp_path).recent_entries() == ()


@pytest.mark.parametrize("failure", (False, True))
def test_client_clear_updates_detached_view_report_and_history(qt_application, tmp_path, monkeypatch, failure):
    history = LocalHistory(tmp_path)
    old = entry(protocol="modbus-tcp", endpoint="host:502")
    history.append_entry(old)
    bridge = IoBridge()
    window = MainWindow(bridge, (DesktopProtocol(
        ProtocolRegistration("modbus-tcp", "Modbus", lambda *_: None), ModbusTcpForm),), history)
    try:
        window.log_table.selectRow(0)
        window.journal_view.open_window()
        window._open_diagnostics()
        if failure:
            monkeypatch.setattr(history, "clear_log", lambda **_: (_ for _ in ()).throw(OSError("磁盘失败")))
        window.journal_view.clear_button.click()
        if failure:
            assert tuple(window.journal.items) == (old,)
            assert "清空日志失败" in window.feedback.text()
            assert history.recent_entries() == (old,)
        else:
            assert window.journal.rowCount() == 0
            assert window.detail.toPlainText() == ""
            assert window.report_log().entries == ()
            assert window._diagnostics is None
            assert LocalHistory(tmp_path).recent_entries() == ()
            newer = entry(2)
            window._append_journal(newer)
            assert tuple(window.journal.items) == (newer,)
            assert window.report_log().entries == (newer,)
    finally:
        if window._diagnostics:
            window._diagnostics.close()
        window.journal_view._dialog.close()
        bridge.join()
        window.close()
        window.deleteLater()
        qt_application.processEvents()


@pytest.mark.parametrize("failure", (False, True))
def test_server_clear_barrier_handles_inflight_write_and_new_requests(qt_application, tmp_path, failure):
    storage = ServerStorage(tmp_path)
    entered, release = Event(), Event()
    original_append, original_clear = storage.append_events, storage.clear_log
    def blocked_append(events):
        entered.set()
        assert release.wait(5)
        return original_append(events)
    storage.append_events = blocked_append
    if failure:
        def failed_clear(**scope):
            raise OSError("磁盘失败")
        storage.clear_log = failed_clear
    bridge = ServerIoBridge(partial(ServerSession, registration=SERVER_PROTOCOLS[0].registration),
                            ServerSession.stop, storage)
    pane = ModbusServerPane(bridge, storage)
    try:
        bridge._emit(request(1))
        assert entered.wait(2)
        bridge._emit(request(2))
        pane.journal_view.clear_button.click()
        assert not pane.journal_view.clear_button.isEnabled()
        bridge._emit(request(3))
        release.set()
        wait_until(qt_application, lambda: pane.journal_view.clear_button.isEnabled() and any(
            event.payload.request_id == 3 for event in pane.requests.items))
        expected = (3, 2, 1) if failure else (3,)
        assert tuple(event.payload.request_id for event in pane.requests.items) == expected
        assert tuple(event.payload.request_id for event in storage.recent_events()) == expected
        if failure:
            assert "清空日志失败" in pane.feedback.text()
            storage.clear_log = original_clear
        pane.journal_view.clear_button.click()
        wait_until(qt_application, lambda: pane.journal_view.clear_button.isEnabled())
        assert pane.requests.rowCount() == 0
        assert storage.recent_events() == ()
        bridge._emit(request(4))
        wait_until(qt_application, lambda: pane.requests.rowCount() == 1)
        assert pane.requests.items[0].payload.request_id == 4
    finally:
        release.set()
        bridge.join()
        qt_application.processEvents()
        pane.close()
        pane.deleteLater()


def test_project_server_clear_is_scoped_and_report_forgets_old_logs(qt_application, tmp_path):
    from all_protocol.project import Project, ProjectWorkspace, ProjectServerStorage
    local = ServerStorage(tmp_path)
    old, other = request(1), request(2, "127.0.0.1:1503")
    local.append_events((old, other))
    storage = ProjectServerStorage(ProjectWorkspace(Project("test")), "modbus", local)
    bridge = ServerIoBridge(partial(ServerSession, registration=SERVER_PROTOCOLS[0].registration),
                            ServerSession.stop, storage)
    pane = ModbusServerPane(bridge, storage)
    try:
        bridge._emit(request(3))
        wait_until(qt_application, lambda: len(pane.report_log().entries) == 1)
        pane.request_table.selectRow(0)
        pane.journal_view.clear_button.click()
        wait_until(qt_application, lambda: pane.journal_view.clear_button.isEnabled())
        assert pane.report_log().entries == ()
        assert pane.detail.toPlainText() == ""
        assert local.recent_events() == (other,)
    finally:
        bridge.join()
        qt_application.processEvents()
        pane.close()
        pane.deleteLater()


def test_project_client_clear_preserves_other_device_view_and_report(qt_application, tmp_path):
    from all_protocol.application.session_manager import SessionManager
    from all_protocol.project import Project, ProjectDevice, ProjectHistory, ProjectWorkspace
    fields = dict(host="127.0.0.1", port=502, timeout_ms=1000, unit_id=1, area="HR", offset=0,
                  count=1, data_type="UInt16", order="big")
    profile = ConnectionProfile("modbus-tcp", "127.0.0.1:502", fields, 1000)
    second = replace(profile, endpoint="127.0.0.2:502", fields=fields | dict(host="127.0.0.2"))
    workspace = ProjectWorkspace(Project("test", devices=(ProjectDevice("a", "A", profile), ProjectDevice("b", "B", second))))
    local = LocalHistory(tmp_path)
    history = ProjectHistory(workspace, local)
    first = entry(device_id="a", protocol=profile.protocol, endpoint=profile.endpoint)
    other = entry(2, device_id="b", protocol=second.protocol, endpoint=second.endpoint)
    local.append_entry(first)
    local.append_entry(other)
    bridge = IoBridge(SessionManager, SessionManager.shutdown)
    window = MainWindow(bridge, (DesktopProtocol(
        ProtocolRegistration("modbus-tcp", "Modbus", lambda *_: None), ModbusTcpForm),), history)
    try:
        window._active_device_id = "a"
        window._show_device_journal()
        window.journal_view.clear_button.click()
        assert window.journal.rowCount() == 0
        assert window.report_log().entries == (other,)
        assert local.recent_entries() == (other,)
        window._active_device_id = "b"
        window._show_device_journal()
        assert tuple(window.journal.items) == (other,)
        assert workspace.project.devices[0].profile == profile
    finally:
        bridge.join()
        qt_application.processEvents()
        window.close()
        window.deleteLater()


def test_clear_while_listening_keeps_tcp_service_available(qt_application, tmp_path):
    import socket
    from all_protocol.adapters.modbus_tcp.model import Area
    from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, Station
    from all_protocol.application.server_contracts import ListenerState
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    storage = ServerStorage(tmp_path)
    storage.save_plan(ServerPlan(port=port, stations=(Station(1).add_points(Area.HR, 0, 1, 42),)))
    bridge = ServerIoBridge(partial(ServerSession, registration=SERVER_PROTOCOLS[0].registration), ServerSession.stop, storage)
    pane = ModbusServerPane(bridge, storage)
    try:
        pane.listen.click()
        wait_until(qt_application, lambda: pane.state == ListenerState.LISTENING and pane.requests.rowCount() > 0)
        pane.journal_view.clear_button.click()
        wait_until(qt_application, lambda: pane.journal_view.clear_button.isEnabled())
        assert pane.requests.rowCount() == 0
        assert pane.state == ListenerState.LISTENING
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            connection.sendall(bytes.fromhex("0001 0000 0006 01 03 0000 0001"))
            with connection.makefile("rb") as stream:
                assert stream.read(11) == bytes.fromhex("0001 0000 0005 01 03 02 002a")
        wait_until(qt_application, lambda: pane.requests.rowCount() == 1)
        assert len(storage.recent_events()) == 1
    finally:
        bridge.join()
        qt_application.processEvents()
        pane.close()
        pane.deleteLater()
