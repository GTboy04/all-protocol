from ..adapters.modbus_rtu.server_model import RtuServerPlan
from ..application.server_contracts import ListenerState
from .modbus_server_pane import ModbusServerPane
from .modbus_rtu_form import SerialConnectionForm


class ModbusRtuServerPane(ModbusServerPane):
    default_plan_type = RtuServerPlan
    max_unit_id = 247
    independent_stations = False

    def _settings(self):
        # Reuse listener, scenario, faults and point controls with serial settings.
        from ..adapters.modbus_tcp.server_model import ServerPlan
        saved = self._saved_plan
        self._saved_plan = ServerPlan()
        panel = super()._settings()
        self._saved_plan = saved
        controls = panel.layout().itemAt(0).layout()
        for index in (0, 1):
            group = controls.itemAt(index).layout()
            for row in range(group.count()):
                group.itemAt(row).widget().hide()
        self.serial = SerialConnectionForm()
        self.serial.restore(saved.config)
        panel.layout().insertWidget(0, self.serial)
        for field in (self.serial.serial_port, self.serial.parity):
            field.currentTextChanged.connect(self._refresh)
        for field in (self.serial.baudrate, self.serial.stopbits, self.serial.timeout):
            field.valueChanged.connect(self._refresh)
        return panel

    def _load_points(self, plan):
        self.serial.restore(plan.config)
        super()._load_points(plan)

    def _load(self):
        # Base _load expects TCP fields; keep serial plans separate from those widgets.
        from PySide6.QtCore import Qt
        try:
            self._saved_plan = self._storage.load_plan()
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._load_error = True
            self._feedback(f"无法读取 RTU 方案 · {error}")
        self._load_points(self._saved_plan)
        try:
            self.requests.reset(self._storage.recent_events())
        except OSError as error:
            self._feedback(f"无法读取请求日志 · {error}")
        self.bridge.event.connect(self._handle_event, Qt.ConnectionType.QueuedConnection)
        self._refresh()

    def _plan(self):
        # Building a plan must also work while the user is typing a COM name.
        return RtuServerPlan(self.serial.serial_port.currentText().strip(), self.serial.baudrate.value(),
                             self.serial.parity.currentData(), self.serial.stopbits.value(),
                             self.serial.timeout.value(), self._stations)

    @property
    def configured_endpoint(self):
        plan = self._plan()
        return f"{plan.serial_port} · {plan.baudrate}/8{plan.parity}{plan.stopbits}"

    def _refresh(self, *_):
        super()._refresh()
        self.serial.setEnabled(self.state == ListenerState.STOPPED)

    def show_workspace_values(self, current):
        super().show_workspace_values(current)
        self.serial.setVisible(not current)
