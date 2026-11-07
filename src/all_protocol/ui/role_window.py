from pathlib import Path
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from ..application.contracts import ClientError
from ..application.session_manager import SessionManager
from ..project import Project, ProjectFile, ProjectHistory, ProjectServerStorage, ProjectWorkspace, decode_project, project_record
from ..storage import LocalHistory, ServerStorage, S7ServerStorage
from .io_bridge import IoBridge
from .main_window import MainWindow, _heading, _panel
from .server_workbench import ServerWorkbench


class RoleWindow(QWidget):
    def __init__(self, protocols, data_dir: Path):
        super().__init__()
        self.setWindowTitle("工业协议调试台")
        self.setObjectName("workspace")
        self.resize(800, 400)
        self._protocols = protocols
        self._data_dir = data_dir
        self._workbenches = {}
        self.workspace: ProjectWorkspace | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        masthead = QFrame()
        masthead.setObjectName("masthead")
        header = QHBoxLayout(masthead)
        header.setContentsMargins(18, 12, 18, 12)
        title = QLabel("工业协议调试台")
        title.setObjectName("title")
        header.addWidget(title)
        layout.addWidget(masthead)

        body = QVBoxLayout()
        body.setContentsMargins(24, 20, 24, 24)
        body.setSpacing(14)
        self.project_status = QLabel()
        self.project_status.setTextFormat(Qt.TextFormat.PlainText)
        self.project_status.setWordWrap(True)
        body.addWidget(self.project_status)
        project_actions = QHBoxLayout()
        self.project_buttons = {}
        for text, action in (("新建项目", self._new_project), ("打开项目…", self._open_project),
                             ("保存项目", self._save_project), ("另存为…", self._save_project_as),
                             ("从本地配置新建…", self._import_local), ("本地调试", self._local_mode)):
            button = QPushButton(text)
            button.clicked.connect(action)
            self.project_buttons[text] = button
            project_actions.addWidget(button)
        body.addLayout(project_actions)
        self._refresh_project()
        body.addWidget(_heading("选择调试模式"))
        cards = QHBoxLayout()
        cards.setSpacing(14)
        cards.addWidget(self._card("客户端", "本机  ──请求──→  设备", "连接设备，读取或写入点位。",
                                   self._open_client), 1)
        cards.addWidget(self._card("服务端", "上位机  ──请求──→  本机", "同时监听 Modbus TCP 和 Siemens S7。",
                                   self._open_server), 1)
        body.addLayout(cards, 1)
        layout.addLayout(body, 1)

    def _card(self, role, direction, description, action):
        card, layout = _panel()
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(_heading(role))
        arrow = QLabel(direction)
        arrow.setObjectName("permissions")
        layout.addWidget(arrow)
        layout.addWidget(QLabel(description))
        layout.addStretch()
        button = QPushButton(f"进入{role}")
        button.setObjectName("primary")
        button.clicked.connect(action)
        layout.addWidget(button)
        return card

    def _open_client(self):
        def create():
            bridge = (IoBridge(SessionManager, SessionManager.shutdown) if self.workspace is not None else IoBridge())
            try:
                history = LocalHistory(self._data_dir)
                if self.workspace is not None:
                    history = ProjectHistory(self.workspace, history)
                window = MainWindow(bridge, self._protocols, history, self._return_home)
                if self.workspace is not None:
                    window.before_close = self._capture_client
                return window, bridge
            except Exception:
                bridge.join()
                raise

        self._open("client", create)

    def _open_server(self):
        def create():
            storages = None
            if self.workspace is not None:
                storages = (
                    ProjectServerStorage(self.workspace, "modbus-tcp", ServerStorage(self._data_dir)),
                    ProjectServerStorage(self.workspace, "s7", S7ServerStorage(self._data_dir)),
                )
            workbench = ServerWorkbench(self._data_dir, self._return_home, storages)
            return workbench, workbench

        self._open("server", create)

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
        for name in ("保存项目", "另存为…", "本地调试"):
            self.project_buttons[name].setEnabled(workspace is not None)

    def _project_error(self, error):
        QMessageBox.warning(self, "项目操作失败", str(error))

    def _can_switch(self):
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
        client = self._workbenches.get("client")
        if client is not None:
            if client[0]._pending_removals:
                raise ValueError("正在清理待移除设备，请完成后再保存项目")
            for profile in reversed(client[0].configuration_profiles()):
                project = project.remember(profile)
        server = self._workbenches.get("server")
        if server is not None:
            tabs = server[0].tabs
            project = replace(project, modbus=tabs.widget(0)._plan(), s7=tabs.widget(1)._plan())
        # Validate the whole snapshot before mutating workspace state.
        decode_project(project_record(project))
        return project

    def _capture_client(self):
        try:
            project = self.workspace.project
            pending = {profile.key for profile in self._workbenches["client"][0]._pending_removals.values()}
            for profile in reversed(self._workbenches["client"][0].configuration_profiles()):
                if profile.key not in pending:
                    project = project.remember(profile)
            self.workspace.project = project
        except (ValueError, ClientError, KeyError, TypeError) as error:
            self._project_error(error)
            return False
        return True

    def _save_project(self, _checked=False, *, save_as=False):
        if self.workspace is None:
            return False
        path = None if save_as else self.workspace.file.path
        if path is None:
            selected, _ = QFileDialog.getSaveFileName(self, "保存项目", self.workspace.project.name + ".allprotocol.json",
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
        server = self._workbenches.get("server")
        if server is not None:
            for index in range(2):
                pane = server[0].tabs.widget(index)
                pane._saved_plan = pane._plan()
                pane._load_error = False
                pane._refresh()
        self._refresh_project()
        return True

    def _save_project_as(self):
        return self._save_project(save_as=True)

    def _confirm_project(self):
        if self.workspace is None:
            return True
        try:
            if self._snapshot() == self.workspace.file.saved:
                return True
        except (ValueError, ClientError, KeyError, TypeError):
            pass  # Invalid edits are also unsaved; user may discard them.
        prompt = QMessageBox(QMessageBox.Icon.Question, "未保存的项目", "如何处理当前项目的修改？", parent=self)
        save = prompt.addButton("保存项目", QMessageBox.ButtonRole.AcceptRole)
        discard = prompt.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
        cancel = prompt.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        prompt.setDefaultButton(cancel)
        prompt.exec()
        return prompt.clickedButton() == discard or (prompt.clickedButton() == save and self._save_project())

    def join(self):
        for _, owner in tuple(self._workbenches.values()):
            owner.join()

    def closeEvent(self, event):
        if not self._confirm_project():
            event.ignore()
            return
        if self.workspace is not None:
            # The project prompt handled all edits; avoid a second per-pane prompt.
            server = self._workbenches.get("server")
            if server is not None:
                for index in range(2):
                    pane = server[0].tabs.widget(index)
                    pane._saved_plan = pane._plan()
            client = self._workbenches.get("client")
            if client is not None:
                client[0].before_close = None
        server = self._workbenches.get("server")
        if server is not None and not server[0].request_close(confirm_listeners=False):
            event.ignore()
            return
        client = self._workbenches.get("client")
        if client is not None:
            client[0].close()
        event.accept()
