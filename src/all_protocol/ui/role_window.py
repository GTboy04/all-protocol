from pathlib import Path
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ..application.contracts import ClientError
from ..application.session_manager import SessionManager
from ..project import Project, ProjectFile, ProjectHistory, ProjectServerStorage, ProjectWorkspace, decode_project, project_record
from ..server_protocols import SERVER_PROTOCOLS
from ..storage import LocalHistory
from .io_bridge import IoBridge
from .main_window import MainWindow, _heading, _panel
from .project_window import ProjectWindow
from .server_workbench import ServerWorkbench


class RoleWindow(QWidget):
    def __init__(self, protocols, data_dir: Path, server_protocols=SERVER_PROTOCOLS):
        super().__init__()
        self.setWindowTitle("工业协议调试台")
        self.setObjectName("workspace")
        self.resize(900, 660)
        self.setMinimumSize(640, 480)
        self._protocols = protocols
        self._server_protocols = tuple(server_protocols)
        if len({item.registration.key for item in self._server_protocols}) != len(self._server_protocols):
            raise ValueError("服务端协议键重复")
        self._data_dir = data_dir
        self._workbenches = {}
        self.workspace: ProjectWorkspace | None = None
        self.project_window: ProjectWindow | None = None
        self._discarded_workspace_edits: Project | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        masthead = QFrame()
        masthead.setObjectName("masthead")
        header = QHBoxLayout(masthead)
        header.setContentsMargins(24, 16, 24, 16)
        mark = QLabel()
        mark.setFixedSize(32, 32)
        mark.setPixmap(self.windowIcon().pixmap(32, 32))
        header.addWidget(mark)
        title = QLabel("工业协议调试台")
        title.setObjectName("title")
        header.addWidget(title, 1)
        layout.addWidget(masthead)

        content = QWidget(objectName="workspace")
        body = QVBoxLayout(content)
        body.setContentsMargins(24, 24, 24, 24)
        body.setSpacing(16)
        body.addWidget(QLabel("开始调试", objectName="pageTitle"))
        introduction = QLabel("在项目中管理设备与点表，或直接开始本地调试。", objectName="resultContext")
        introduction.setWordWrap(True)
        body.addWidget(introduction)
        project_panel, project_layout = _panel()
        project_layout.addWidget(_heading("项目"))
        self.project_status = QLabel()
        self.project_status.setTextFormat(Qt.TextFormat.PlainText)
        self.project_status.setWordWrap(True)
        self.project_status.setObjectName("resultContext")
        project_layout.addWidget(self.project_status)
        project_actions = QHBoxLayout()
        project_actions.setSpacing(8)
        secondary_actions = QHBoxLayout()
        secondary_actions.setSpacing(8)
        self.project_buttons = {}
        for text, action in (("新建项目", self._new_project), ("打开项目…", self._open_project),
                             ("保存项目", self._save_project), ("另存为…", self._save_project_as),
                             ("从本地配置新建…", self._import_local), ("本地调试", self._local_mode)):
            button = QPushButton(text)
            button.clicked.connect(action)
            self.project_buttons[text] = button
            (project_actions if text in ("新建项目", "打开项目…", "从本地配置新建…") else secondary_actions).addWidget(button)
        for name, shortcut in (("新建项目", "Ctrl+N"), ("打开项目…", "Ctrl+O"),
                               ("保存项目", "Ctrl+S"), ("另存为…", "Ctrl+Shift+S")):
            self.project_buttons[name].setShortcut(shortcut)
            self.project_buttons[name].setToolTip(f"{name} ({shortcut})")
        self.project_buttons["新建项目"].setObjectName("primary")
        project_actions.addStretch()
        secondary_actions.addStretch()
        project_layout.addLayout(project_actions)
        project_layout.addLayout(secondary_actions)
        self.project_entry = QPushButton("进入项目工作区")
        self.project_entry.setObjectName("primary")
        self.project_entry.clicked.connect(self._open_workspace)
        project_layout.addWidget(self.project_entry, alignment=Qt.AlignmentFlag.AlignLeft)
        body.addWidget(project_panel)
        self._mode_heading = _heading("本地调试")
        body.addWidget(self._mode_heading)
        self._local_cards = QWidget()
        cards = QHBoxLayout(self._local_cards)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setSpacing(16)
        cards.addWidget(self._card("客户端", "本机  ──请求──→  设备", "连接设备，读取或写入点位。",
                                   self._open_client), 1)
        server_labels = "、".join(item.registration.label for item in self._server_protocols)
        cards.addWidget(self._card("服务端", "上位机  ──请求──→  本机", f"可同时监听 {server_labels}。",
                                   self._open_server), 1)
        body.addWidget(self._local_cards, 1)
        body.addStretch()
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self._refresh_project()

    def _card(self, role, direction, description, action):
        card, layout = _panel()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(_heading(role))
        arrow = QLabel(direction)
        arrow.setObjectName("permissions")
        arrow.setWordWrap(True)
        layout.addWidget(arrow)
        detail = QLabel(description)
        detail.setWordWrap(True)
        layout.addWidget(detail)
        layout.addStretch()
        button = QPushButton(f"进入{role}")
        button.clicked.connect(action)
        layout.addWidget(button)
        return card

    def _open_client(self):
        if self.workspace is not None:
            self._open_workspace("client")
            return

        def create():
            bridge = IoBridge()
            try:
                history = LocalHistory(self._data_dir)
                return MainWindow(bridge, self._protocols, history, self._return_home), bridge
            except Exception:
                bridge.join()
                raise

        self._open("client", create)

    def _open_server(self):
        if self.workspace is not None:
            self._open_workspace("server")
            return

        self._open("server", self._create_local_server)

    def _create_local_server(self):
        storages = [(protocol, protocol.create_storage(self._data_dir)) for protocol in self._server_protocols]
        workbench = ServerWorkbench(self._return_home, storages)
        return workbench, workbench

    def _open_workspace(self, view=None):
        if self.workspace is None:
            return
        if self.project_window is None:
            bridge = IoBridge(SessionManager, SessionManager.shutdown)
            servers = None
            opening_project = self.workspace.project
            try:
                client = MainWindow(bridge, self._protocols, ProjectHistory(self.workspace, LocalHistory(self._data_dir)),
                                    embedded=True)
                storages = [(protocol, ProjectServerStorage(self.workspace, protocol.project_field,
                                                            protocol.create_storage(self._data_dir)))
                            for protocol in self._server_protocols if protocol.project_field is not None]
                servers = ServerWorkbench(self._return_home, storages, embedded=True)
                window = ProjectWindow(self.workspace, client, servers, home=self._return_home, save=self._save_project,
                                       confirm_close=lambda parent: self._confirm_workspace_close(parent, opening_project))
            except Exception:
                bridge.join()
                if servers is not None:
                    servers.join()
                raise
            self.project_window = window
            client.before_close = self._capture_client
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            window.destroyed.connect(self._workspace_closed)
        if self.project_window._closing:
            return
        if view == "client":
            self.project_window.show_clients()
        elif view == "server":
            self.project_window.show_servers()
        self._activate(self.project_window)

    def _workspace_closed(self):
        self.project_window = None
        if self._discarded_workspace_edits is not None and self.workspace is not None:
            self.workspace.project = self._discarded_workspace_edits
        self._discarded_workspace_edits = None

    def _confirm_workspace_close(self, parent, opening_project):
        def discard():
            self._discarded_workspace_edits = self.workspace.file.saved or opening_project

        return self._confirm_project(parent=parent, stop_running=parent.has_running_io(), discard_action=discard)

    def _open(self, role, create):
        if role in self._workbenches:
            workbench, _ = self._workbenches[role]
        else:
            workbench, owner = create()
            self._workbenches[role] = workbench, owner
            workbench.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            workbench.destroyed.connect(lambda: self._workbenches.pop(role, None))
        self._activate(workbench)

    @staticmethod
    def _activate(workbench):
        workbench.show()
        if workbench.isMinimized():
            workbench.setWindowState(workbench.windowState() & ~Qt.WindowState.WindowMinimized)
        workbench.raise_()
        workbench.activateWindow()

    def _return_home(self):
        self._refresh_project()
        self._activate(self)

    def _refresh_project(self):
        workspace = self.workspace
        if workspace is None:
            text = "本地调试 · 配置保存在本机"
        else:
            path = str(workspace.file.path) if workspace.file.path else "尚未保存"
            text = f"当前项目：{workspace.project.name} · {path}（修改后请保存项目）"
        self.project_status.setText(text)
        self.project_entry.setVisible(workspace is not None)
        self._mode_heading.setVisible(workspace is None)
        self._local_cards.setVisible(workspace is None)
        for name in ("保存项目", "另存为…", "本地调试"):
            self.project_buttons[name].setEnabled(workspace is not None)

    def _project_error(self, error):
        QMessageBox.warning(self.project_window or self, "项目操作失败", str(error))

    def _can_switch(self):
        if self.project_window is not None:
            self._project_error("请先关闭项目工作区，再切换项目或调试模式。")
            return False
        if self._workbenches:
            self._project_error("请先关闭客户端和服务端窗口，再切换项目或调试模式。")
            return False
        return True

    def _project_name(self):
        name, accepted = QInputDialog.getText(self, "新建项目", "项目名称")
        if not accepted:
            return None
        if not name.strip() or len(name.strip()) > 200:
            self._project_error("项目名称必须为 1–200 字符")
            return None
        return name.strip()

    def _new_project(self):
        if not self._can_switch():
            return
        name = self._project_name()
        if name is not None and self._confirm_project():
            self.workspace = ProjectWorkspace(Project(name))
            self._refresh_project()

    def _import_local(self):
        if not self._can_switch():
            return
        name = self._project_name()
        if name is None:
            return
        try:
            workspace = ProjectWorkspace.from_local(name, self._data_dir)
        except (OSError, ValueError, KeyError, TypeError, ClientError) as error:
            self._project_error(error)
            return
        if self._confirm_project():
            self.workspace = workspace
            self._refresh_project()

    def _open_project(self):
        if not self._can_switch():
            return
        path, _ = QFileDialog.getOpenFileName(self, "打开项目", "", "AllProtocol 项目 (*.allprotocol.json);;JSON (*.json)")
        if not path:
            return
        file = ProjectFile()
        try:
            project = file.open(Path(path))
        except (OSError, ValueError) as error:
            self._project_error(error)
            return
        if self._confirm_project():
            self.workspace = ProjectWorkspace(project, file)
            self._refresh_project()

    def _local_mode(self):
        if self._can_switch() and self._confirm_project():
            self.workspace = None
            self._refresh_project()

    def _snapshot(self):
        project = self.workspace.project
        window = self.project_window
        if window is not None and not window._closing:
            if window.client._pending_removals:
                raise ValueError("正在清理待移除设备，请完成后再保存项目")
            for profile in reversed(window.client.configuration_profiles()):
                project = project.remember(profile)
            project = replace(project, layout=window.layout_snapshot(), **window.servers.project_plans())
        # Validate the whole snapshot before mutating workspace state.
        decode_project(project_record(project))
        return project

    def _capture_client(self):
        try:
            project = self.workspace.project
            client = self.project_window.client
            pending = {profile.key for profile in client._pending_removals.values()}
            for profile in reversed(client.configuration_profiles()):
                if profile.key not in pending:
                    project = project.remember(profile)
            self.workspace.project = project
        except (ValueError, ClientError, KeyError, TypeError) as error:
            self._project_error(error)
            return False
        return True

    def _save_project(self, _checked=False, *, save_as=False):
        if self.workspace is None or (self.project_window is not None and self.project_window._closing):
            return False
        path = None if save_as else self.workspace.file.path
        if path is None:
            selected, _ = QFileDialog.getSaveFileName(self.project_window or self, "保存项目",
                                                     self.workspace.project.name + ".allprotocol.json",
                                                     "AllProtocol 项目 (*.allprotocol.json)")
            if not selected:
                return False
            path = Path(selected)
            if not path.name.lower().endswith(".json"):
                path = path.with_name(path.name + ".allprotocol.json")
                if path.exists() and QMessageBox.question(
                    self, "替换项目文件", f"文件已存在，是否替换？\n{path}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                ) != QMessageBox.StandardButton.Yes:
                    return False
        try:
            project = self._snapshot()
            self.workspace.file.save(project, path)
        except (OSError, ValueError, ClientError, KeyError, TypeError) as error:
            self._project_error(error)
            return False
        self.workspace.project = project
        if self.project_window is not None:
            self.project_window.mark_layout_saved()
            self.project_window.servers.mark_plans_saved()
            self.project_window.client._sync_profiles()
        self._refresh_project()
        return True

    def _save_project_as(self):
        return self._save_project(save_as=True)

    def _confirm_project(self, *, parent=None, stop_running=False, discard_action=None):
        if self.workspace is None:
            return True
        try:
            if self._snapshot() == self.workspace.file.saved:
                if not stop_running:
                    return True
                prompt = QMessageBox(QMessageBox.Icon.Question, "关闭项目工作区",
                                     "关闭后将断开设备并停止监听。", parent=parent or self)
                stop = prompt.addButton("关闭工作区", QMessageBox.ButtonRole.AcceptRole)
                cancel = prompt.addButton("取消", QMessageBox.ButtonRole.RejectRole)
                prompt.setDefaultButton(cancel)
                prompt.exec()
                return prompt.clickedButton() == stop
        except (ValueError, ClientError, KeyError, TypeError):
            pass  # Invalid edits are also unsaved; user may discard them.
        message = "如何处理当前项目的修改？"
        if stop_running:
            message += "\n关闭后将断开设备并停止监听。"
        prompt = QMessageBox(QMessageBox.Icon.Question, "未保存的项目", message, parent=parent or self)
        save = prompt.addButton("保存项目", QMessageBox.ButtonRole.AcceptRole)
        discard = prompt.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
        cancel = prompt.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        prompt.setDefaultButton(cancel)
        prompt.exec()
        if prompt.clickedButton() == discard:
            if discard_action is not None:
                discard_action()
            return True
        return prompt.clickedButton() == save and self._save_project()

    def join(self):
        if self.project_window is not None:
            self.project_window.join()
        for _, owner in tuple(self._workbenches.values()):
            owner.join()

    def closeEvent(self, event):
        if self.project_window is not None:
            event.setAccepted(self.project_window.request_close())
            return
        if not self._confirm_project():
            event.ignore()
            return
        server = self._workbenches.get("server")
        if server is not None and not server[0].request_close(confirm_listeners=False):
            event.ignore()
            return
        client = self._workbenches.get("client")
        if client is not None:
            client[0].close()
        event.accept()
