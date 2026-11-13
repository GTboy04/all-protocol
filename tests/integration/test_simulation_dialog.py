import pytest
from PySide6.QtWidgets import QApplication, QDialog

from all_protocol.adapters.modbus_tcp.model import Area
from all_protocol.adapters.modbus_tcp.server_model import ServerPlan, ServerPoint, Station
from all_protocol.adapters.s7.model import Area as S7Area, DataType, S7Target
from all_protocol.adapters.s7.server_model import S7ServerPlan, S7ServerPoint
from all_protocol.application.simulation import RuleKind
from all_protocol.ui.simulation_dialog import RuleDialog


@pytest.mark.parametrize("protocol", ("modbus", "s7"))
def test_sine_dialog_explains_invalid_range_and_saves_corrected_rule(monkeypatch, protocol):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    if protocol == "modbus":
        plan = ServerPlan(stations=(Station(1, points=(ServerPoint(Area.HR, 3507, 0, "heartbeat_wcs"),)),))
    else:
        plan = S7ServerPlan(points=(S7ServerPoint(S7Target(S7Area.DB, 1, 0, DataType.WORD), 0),))
    saved = []
    dialog = RuleDialog(None, next(plan.simulation_points()), saved.append)
    try:
        dialog.show()
        dialog.kind.setCurrentIndex(dialog.kind.findData(RuleKind.SINE.value))
        dialog.value.setText("0")
        dialog.amplitude.setText("1")
        app.processEvents()
        assert dialog.sine_hint.isVisible()
        assert "中心值 ± 幅度" in dialog.sine_hint.text()
        dialog.accept()
        assert not saved
        assert dialog.isVisible()
        assert "-1 ～ 1" in dialog.error.text()
        dialog.value.setText("1")
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert len(saved) == 1 and saved[0].value == saved[0].amplitude == 1
    finally:
        dialog.close()
        dialog.deleteLater()
