"""OPC UA connection, local certificate setup and hierarchical node browsing."""

from dataclasses import asdict

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..adapters.opcua.client import BrowsePoint, discover_endpoints
from ..adapters.opcua.identity import IdentityStore, UaIdentity, fingerprint, load_certificate
from ..adapters.opcua.model import OpcUaConfig, STANDARD_NAMESPACE, UaBrowseTarget, UaTarget, UaType
from ..application.contracts import ClientError, Command, ConnectionState
from ..point_tables import decode_target, target_record
from .widgets import add_field, spinbox
from .write_dialog import WriteDialog


class _Signals(QObject):
    discovered = Signal(object, object, object)


class OpcUaForm:
    def __init__(self, identity_store=None):
        self.identities = identity_store or IdentityStore()
        self.connection = _Connection(self.identities)
        self.query = _Query()
        self.signals = _Signals(self.connection)
        self.signals.discovered.connect(self._discovered, Qt.ConnectionType.QueuedConnection)
        self.connection.discover.clicked.connect(self._discover)
        self._bridge = None

    def bind_io(self, bridge):
        self._bridge = bridge

    def connection_config(self):
        config = OpcUaConfig(self.connection.host.text().strip(), self.connection.security.currentData(),
                             self.connection.identity_id, self.connection.timeout.value())
        config.validate()
        return config, config.endpoint

    def target(self):
        return self.query.target()

    def can_write(self):
        return True

    def profile_fields(self):
        config, _ = self.connection_config()
        return {**asdict(config), **target_record(self.target())}

    def restore_profile(self, fields):
        connection = {key: fields[key] for key in ("url", "security", "identity_id", "timeout_ms")}
        config = OpcUaConfig(**connection)
        config.validate()
        target = decode_target("opcua", {key: fields[key] for key in (
            "namespace_uri", "identifier_type", "identifier", "data_type", "count")})
        self.connection.host.setText(config.url)
        self.connection.security.setCurrentIndex(self.connection.security.findData(config.security))
        self.connection.identity_id = config.identity_id
        self.connection.identity_label.setText(config.identity_id or "尚未配置本机证书")
        self.connection.timeout.setValue(config.timeout_ms)
        self.query.set_target(target)
        self.query.nodes.clear()

    def confirm_write(self, parent, endpoint, target, point=None):
        dialog = WriteDialog(parent, endpoint, target, point)
        try:
            return dialog.command if dialog.exec() == QDialog.DialogCode.Accepted else None
        finally:
            dialog.deleteLater()

    def refresh_state(self, state):
        self.query.browse.setEnabled(state.connection == ConnectionState.ONLINE)

    def handle_read(self, event):
        if isinstance(event.command.target, UaBrowseTarget) and event.points is not None:
            self.query.nodes.clear()
            for point in event.points:
                if isinstance(point, BrowsePoint) and point.reference:
                    ref = point.reference
                    item = QTreeWidgetItem([ref.name, ref.node_class, ref.data_type or "—",
                                            "可写" if ref.writable else "只读 / 不支持", point.address])
                    item.setData(0, Qt.ItemDataRole.UserRole, ref)
                    self.query.nodes.addTopLevelItem(item)
            self.query.hint.setText(f"本层 {len(event.points)} 个节点；双击对象浏览下一层，双击标量变量填入读写目标。")
            self.query.show_nodes.setChecked(True)

    def _discover(self):
        if self._bridge is None:
            return
        try:
            config, _ = self.connection_config()
        except ClientError as error:
            self.connection.discovery_status.setText(str(error))
            return
        self.connection.discover.setEnabled(False)
        self.connection.discovery_status.setText("正在发现端点…")

        async def operation():
            try:
                endpoints, error = await discover_endpoints(config.url, config.timeout_ms), None
            except Exception as failure:
                endpoints, error = (), str(failure)
            self.signals.discovered.emit(config.url, endpoints, error)

        self._bridge.submit(operation())

    def _discovered(self, url, endpoints, error):
        self.connection.discover.setEnabled(True)
        if self.connection.host.text().strip() != url:
            return
        if error:
            self.connection.discovery_status.setText(error)
            return
        self.connection.discovery_status.setText(f"发现 {len(endpoints)} 个端点；尚未建立会话或信任证书。")
        dialog = QDialog(self.connection)
        dialog.setWindowTitle("OPC UA 端点发现")
        dialog.resize(940, 420)
        layout = QVBoxLayout(dialog)
        hint = QLabel("发现结果未经过身份验证。请通过可信渠道核对服务端证书指纹，并在「本机证书…」中配置可信证书。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        rows = QTreeWidget()
        rows.setHeaderLabels(["端点", "策略", "模式", "匿名", "SHA-256 指纹", "应用 URI"])
        for item in endpoints:
            rows.addTopLevelItem(QTreeWidgetItem([item.url, item.policy, item.mode, "是" if item.anonymous else "否",
                                                  item.fingerprint or "无证书", item.application_uri]))
        layout.addWidget(rows)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()


class _Connection(QWidget):
    def __init__(self, store):
        super().__init__()
        self.store, self.identity_id = store, ""
        self.host = QLineEdit()
        self.host.setPlaceholderText("opc.tcp://192.168.0.10:4840/路径")
        self.host.setMinimumWidth(280)
        self.security = QComboBox()
        self.security.addItem("Basic256Sha256 · 签名和加密", "Basic256Sha256/SignAndEncrypt")
        self.security.addItem("无加密 · 匿名（显式选择）", "None/None")
        self.timeout = spinbox(100, 30000, 3000)
        self.discover = QPushButton("发现端点")
        self.certificates = QPushButton("本机证书…")
        self.certificates.clicked.connect(self._certificates)
        self.identity_label = QLabel("尚未配置本机证书")
        self.identity_label.setTextFormat(Qt.TextFormat.PlainText)
        self.discovery_status = QLabel("首版使用匿名身份；安全策略保护通道，节点权限由服务端决定。")
        self.discovery_status.setWordWrap(True)
        self.discovery_status.setTextFormat(Qt.TextFormat.PlainText)
        fields = QHBoxLayout()
        add_field(fields, "端点 URL", self.host, 2)
        add_field(fields, "通道安全", self.security, 1)
        add_field(fields, "超时（ms）", self.timeout)
        controls = QHBoxLayout()
        controls.addWidget(self.discover)
        controls.addWidget(self.certificates)
        controls.addWidget(self.identity_label, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(fields)
        layout.addLayout(controls)
        layout.addWidget(self.discovery_status)

    def _certificates(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("本机 OPC UA 证书配置")
        dialog.resize(740, 390)
        layout = QVBoxLayout(dialog)
        hint = QLabel("选择客户端证书、未加密 PEM 私钥和已核对的服务端证书。项目仅保存配置 ID；私钥与路径留在当前计算机。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        fields = {}
        for key, label in (("certificate", "客户端证书"), ("private_key", "本机私钥"),
                           ("trusted_server", "信任的服务端证书"), ("application_uri", "客户端应用 URI")):
            edit = fields[key] = QLineEdit()
            if key == "application_uri":
                form.addRow(label, edit)
            else:
                row = QHBoxLayout()
                row.addWidget(edit, 1)
                button = QPushButton("选择…")
                button.clicked.connect(lambda _=False, field=edit: self._choose(dialog, field))
                row.addWidget(button)
                form.addRow(label, row)
        try:
            identity = self.store.load(self.identity_id)
        except ClientError:
            pass
        else:
            for key, value in asdict(identity).items():
                fields[key].setText(value)
        layout.addLayout(form)
        generate = QPushButton("生成本机客户端证书和私钥")
        layout.addWidget(generate)
        status = QLabel()
        status.setWordWrap(True)
        status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(status)

        def create():
            try:
                cert, key, uri = self.store.generate()
                for name, value in (("certificate", cert), ("private_key", key), ("application_uri", uri)):
                    fields[name].setText(value)
                status.setText("已生成。请将客户端公钥证书加入服务端信任列表；私钥保留本机。")
            except Exception:
                status.setText("无法生成本机证书，请检查本地数据目录权限。")

        def save():
            try:
                identity = UaIdentity(**{key: edit.text().strip() for key, edit in fields.items()})
                identity_id = self.store.save(identity)
                thumb = fingerprint(load_certificate(identity.trusted_server))
            except Exception:
                status.setText("证书配置无效。请检查证书/私钥文件、密钥匹配、应用 URI 及目录权限。")
                return
            self.identity_id = identity_id
            self.identity_label.setText("服务端 SHA-256 " + thumb)
            self.identity_label.setToolTip(identity_id)
            dialog.accept()

        generate.clicked.connect(create)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    @staticmethod
    def _choose(parent, edit):
        path, _ = QFileDialog.getOpenFileName(parent, "选择本机证书或私钥", "", "证书 / 密钥 (*.der *.pem *.crt *.key);;全部文件 (*)")
        if path:
            edit.setText(path)


class _Query(QWidget):
    changed = Signal()
    command_requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.namespace_uri = QLineEdit(STANDARD_NAMESPACE)
        self.identifier_type = QComboBox()
        for kind, label in (("i", "i 数值"), ("s", "s 文本"), ("g", "g GUID"), ("b", "b 字节")):
            self.identifier_type.addItem(label, kind)
        self.identifier = QLineEdit("2267")
        self.data_type = QComboBox()
        for kind in UaType:
            self.data_type.addItem(kind.value, kind.value)
        self.data_type.setCurrentIndex(self.data_type.findData("Byte"))
        self.count = spinbox(1, 1, 1)
        self.count.hide()
        fields = QHBoxLayout()
        add_field(fields, "命名空间 URI", self.namespace_uri, 2)
        add_field(fields, "标识符类型", self.identifier_type)
        add_field(fields, "节点标识符", self.identifier, 1)
        add_field(fields, "标量类型", self.data_type)
        controls = QHBoxLayout()
        self.browse = QPushButton("浏览此节点的下一层")
        self.root = QPushButton("Objects 根节点")
        self.show_nodes = QPushButton("节点列表")
        self.show_nodes.setCheckable(True)
        self.hint = QLabel("浏览后双击对象展开，或选择受支持的标量变量。数组与复杂类型暂不支持。")
        self.hint.setWordWrap(True)
        controls.addWidget(self.browse)
        controls.addWidget(self.root)
        controls.addWidget(self.show_nodes)
        controls.addWidget(self.hint, 1)
        self.nodes = QTreeWidget()
        self.nodes.setHeaderLabels(["名称", "节点类别", "标量类型", "访问权限", "地址"])
        self.nodes.setMinimumHeight(120)
        self.nodes.setMaximumHeight(190)
        self.nodes.setAccessibleName("OPC UA 节点浏览")
        self.nodes.hide()
        self.show_nodes.toggled.connect(self.nodes.setVisible)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(fields)
        layout.addLayout(controls)
        layout.addWidget(self.nodes)
        for field in (self.namespace_uri, self.identifier):
            field.textChanged.connect(self.changed)
        for field in (self.identifier_type, self.data_type):
            field.currentIndexChanged.connect(self.changed)
        self.browse.clicked.connect(lambda: self.command_requested.emit(Command(UaBrowseTarget(self.target()))))
        self.root.clicked.connect(lambda: self.set_target(UaTarget(STANDARD_NAMESPACE, "i", "85", UaType.Byte)))
        self.nodes.itemDoubleClicked.connect(self._select)

    def target(self):
        return UaTarget(self.namespace_uri.text().strip(), self.identifier_type.currentData(),
                        self.identifier.text(), UaType(self.data_type.currentData()))

    def set_target(self, target):
        self.namespace_uri.setText(target.namespace_uri)
        self.identifier_type.setCurrentIndex(self.identifier_type.findData(target.identifier_type))
        self.identifier.setText(target.identifier)
        self.data_type.setCurrentIndex(self.data_type.findData(target.data_type.value))

    def _select(self, item, _column):
        ref = item.data(0, Qt.ItemDataRole.UserRole)
        if ref.node_class == "Variable" and not ref.data_type:
            self.hint.setText("此变量为数组、复杂类型或无法读取类型，首版不支持。")
            return
        self.set_target(UaTarget(ref.namespace_uri, ref.identifier_type, ref.identifier,
                                UaType(ref.data_type) if ref.data_type else UaType.Byte))
        if ref.node_class != "Variable":
            self.browse.click()
        else:
            self.show_nodes.setChecked(False)
