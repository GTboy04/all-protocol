from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / "assets"

STYLE = """
QWidget {
    color: #182f45;
    font-family: "Microsoft YaHei UI";
    font-size: 13px;
}
QWidget#workspace, QDialog { background: #edf2f6; }
QFrame#masthead { background: #142a40; border-bottom: 2px solid #367980; }
QLabel#title { color: #f5f9fd; font-size: 21px; font-weight: bold; }
QLabel#connectionStatus {
    background: #263e54;
    color: #eef5fb;
    border: 1px solid #496278;
    border-radius: 12px;
    padding: 4px 12px;
    font-weight: bold;
}
QLabel#connectionStatus[tone="online"] {
    background: #154c47;
    color: #b8f6e2;
    border-color: #398775;
}
QLabel#connectionStatus[tone="pending"] {
    background: #493a26;
    color: #ffe6ba;
    border-color: #896b3d;
}
QFrame#panel { background: #ffffff; border: 1px solid #d4dfe8; border-radius: 7px; }
QLabel#sectionTitle { font-size: 15px; font-weight: bold; }
QLabel#fieldLabel { color: #405970; }
QLabel#resultContext { color: #455f75; }
QLabel#permissions {
    color: #3b596d;
    background: #f1f6fa;
    border-radius: 4px;
    padding: 5px 8px;
}
QLabel#feedback { color: #3b596d; padding: 2px 0; }
QLabel#warning { color: #8d470c; }

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
    background: #ffffff;
    border: 1px solid #b9cad8;
    border-radius: 4px;
    selection-background-color: #0f6570;
    selection-color: #ffffff;
}
QLineEdit, QSpinBox, QComboBox { min-height: 22px; padding: 4px 8px; }
QSpinBox QLineEdit { border: 0; background: transparent; padding: 0 0 0 6px; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: #0f6570; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {
    color: #556b7d;
    background: #f0f4f7;
    border-color: #d4dfe8;
}
QSpinBox::up-button, QSpinBox::down-button, QComboBox::drop-down {
    width: 19px;
    background: #f1f6fa;
    border: 0;
    border-left: 1px solid #d4dfe8;
}
QSpinBox::up-button:enabled:hover, QSpinBox::down-button:enabled:hover, QComboBox::drop-down:enabled:hover {
    background: #deedf2;
}
QSpinBox::up-button { border-top-right-radius: 3px; }
QSpinBox::down-button { border-bottom-right-radius: 3px; }
QComboBox::drop-down { border-top-right-radius: 3px; border-bottom-right-radius: 3px; }
QSpinBox::up-arrow { image: url("@assets/chevron-up.svg"); width: 9px; height: 6px; }
QSpinBox::down-arrow, QComboBox::down-arrow { image: url("@assets/chevron-down.svg"); width: 9px; height: 6px; }
QComboBox { padding-right: 25px; }
QComboBox QAbstractItemView {
    background: #ffffff;
    selection-background-color: #0f6570;
    selection-color: #ffffff;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #b9cad8;
    border-radius: 4px;
    min-height: 22px;
    padding: 4px 13px;
    font-weight: bold;
}
QPushButton:hover { background: #edf4f8; border-color: #8da9be; }
QPushButton:pressed { background: #dce8f0; }
QPushButton:disabled { background: #f0f4f7; border-color: #d4dfe8; color: #5d7283; }
QPushButton#primary:enabled { background: #0f6570; border-color: #0f6570; color: #ffffff; }
QPushButton#primary:hover { background: #0c5660; }
QPushButton#primary:pressed { background: #09454f; }
QPushButton#write:enabled { background: #fff8eb; border-color: #d7b68b; color: #8d470c; }
QPushButton#write:hover { background: #ffefd4; }
QPushButton#write:pressed { background: #f7e1bf; }
QPushButton:focus, QPushButton#primary:focus, QPushButton#write:focus { border: 2px solid #142a40; }

QTableView {
    background: #ffffff;
    alternate-background-color: #f3f7fa;
    border: 1px solid #d4dfe8;
    selection-background-color: #d2e9ed;
    selection-color: #123b48;
}
QTableView:focus { border-color: #0f6570; }
QTableView::item { padding: 0 8px; }
QHeaderView { background: #eaf1f6; }
QHeaderView::section {
    background: #eaf1f6;
    color: #3b536a;
    border: 0;
    border-bottom: 1px solid #d4dfe8;
    padding: 4px 8px;
    font-weight: bold;
}
QPlainTextEdit { padding: 6px; }
QPlainTextEdit#packetDetail {
    background: #182f45;
    color: #e6f2fa;
    placeholder-text-color: #b4c8d8;
    border-color: #182f45;
    font-family: "Consolas", "Microsoft YaHei UI";
    padding: 6px 8px;
}
QPlainTextEdit#packetDetail:focus { border-color: #55c0c3; }
QSplitter::handle:vertical { background: #d4dfe8; margin: 3px 0; }
QSplitter::handle:vertical:hover { background: #6799a3; }
""".replace("@assets", ASSETS.as_posix())
