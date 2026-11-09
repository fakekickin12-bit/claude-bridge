"""Local Windows device enrollment. Pairing secrets never leave this window/file."""
import argparse
from datetime import datetime
import io
from pathlib import Path
import re
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel,
                              QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                              QTextEdit, QVBoxLayout, QWidget)

import core
from device_auth import pairing_url, issue_pairing_code, list_devices, revoke_device


def date_text(value):
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M") if value else "尚未使用"


class DevicePanel(QWidget):
    def __init__(self, data_dir=None, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir) if data_dir is not None else core.DATA_DIR
        self.setObjectName("devicePanel")
        self.setStyleSheet("""
            QWidget#devicePanel { background:#edf3f8; color:#163e66; }
            QLabel { color:#163e66; font-size:13px; }
            QPushButton { padding:8px 12px; border:1px solid #c4d5e7; border-radius:5px; background:white; color:#164b80; }
            QPushButton#pair { background:#286db7; color:white; }
            QTableWidget, QTextEdit { background:white; border:1px solid #c4d5e7; border-radius:5px; color:#173b5b; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)
        title = QLabel("登记设备后，手机和 iPad 可自动登录")
        title.setStyleSheet("font-size:20px;font-weight:700;padding:3px 0;")
        layout.addWidget(title)
        self.origin_label = QLabel("")
        layout.addWidget(self.origin_label)
        hint = QLabel("在这里生成配对码，用手机相机扫码打开，填写设备名称并登记。\n"
                      "每个配对码限登记一台设备，有效期 24 小时。设备配对和撤销立即生效，无需点击保存设置。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        actions = QHBoxLayout()
        pair = QPushButton("生成新设备配对码")
        self.pair_button = pair
        pair.setObjectName("pair")
        pair.clicked.connect(self.pair)
        refresh = QPushButton("刷新设备")
        refresh.clicked.connect(self.refresh)
        revoke = QPushButton("撤销选中设备")
        revoke.clicked.connect(self.revoke)
        actions.addWidget(pair)
        actions.addWidget(refresh)
        actions.addStretch()
        actions.addWidget(revoke)
        layout.addLayout(actions)
        pair_layout = QHBoxLayout()
        self.qr = QLabel("生成配对码后显示二维码")
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setWordWrap(True)
        self.qr.setFixedSize(230, 230)
        self.note = QTextEdit()
        self.note.setReadOnly(True)
        self.note.setPlaceholderText("配对信息仅保存在此 Windows 账户下。")
        self.note.setMaximumHeight(230)
        pair_layout.addWidget(self.qr)
        pair_layout.addWidget(self.note, 1)
        layout.addLayout(pair_layout)
        layout.addWidget(QLabel("已登记设备"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["设备名称", "登记时间", "最近使用", "状态"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setMinimumHeight(120)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QLabel("设备凭证保存在登记时使用的浏览器中。换浏览器或清除网站数据后，需要重新登记。")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.periodic_refresh)
        self.refresh_origin()

    def refresh_origin(self):
        try:
            self.origin = pairing_url(self.data_dir)
        except core.BridgeError as exc:
            self.origin = ""
            self.origin_label.setText("尚未配置手机 HTTPS 入口")
            self.pair_button.setEnabled(False)
            self.status.setText(str(exc))
        else:
            self.origin_label.setText(self.origin)
            self.pair_button.setEnabled(True)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.load_pairing_info()
        self.refresh_origin()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)

    def periodic_refresh(self):
        if not self.isVisible():
            self.timer.stop()
            return
        self.refresh()
        self.refresh_origin()

    def refresh(self):
        try:
            selected = self.table.currentItem()
            selected_id = self.table.item(selected.row(), 0).data(Qt.UserRole) if selected else None
            rows = list_devices(self.data_dir)
            self.table.setRowCount(len(rows))
            for row, device in enumerate(rows):
                expired = device.get("expires_at", 0) < datetime.now().timestamp()
                state = "已撤销" if device.get("revoked") else "已过期" if expired else "允许自动登录"
                values = [device["label"], date_text(device["created_at"]), date_text(device.get("last_seen")), state]
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    if column == 0:
                        item.setData(Qt.UserRole, device["id"])
                    self.table.setItem(row, column, item)
                if device["id"] == selected_id:
                    self.table.selectRow(row)
            self.status.setText("未登记的浏览器无法访问 Claude Bridge。")
        except Exception:
            self.status.setText("暂时无法读取设备列表，请稍后刷新。")

    def display_pairing_info(self, path):
        text = path.read_text(encoding="utf-8")
        match = re.search(r"配对码：([^\s]+)", text)
        if not match:
            raise ValueError("pairing note format")
        import qrcode
        from qrcode.image.pil import PilImage
        output = io.BytesIO()
        origin = pairing_url(self.data_dir)
        if origin not in text.splitlines():
            raise ValueError("The saved pairing note belongs to another origin")
        qrcode.make(origin + "/#pair=" + match.group(1), image_factory=PilImage).save(output, format="PNG")
        pixmap = QPixmap()
        if not pixmap.loadFromData(output.getvalue(), "PNG"):
            raise ValueError("pairing QR format")
        self.qr.setPixmap(pixmap.scaled(self.qr.size(), Qt.KeepAspectRatio, Qt.FastTransformation))
        self.note.setPlainText(text)

    def load_pairing_info(self):
        path = self.data_dir / "mobile" / "设备配对信息.txt"
        if not path.is_file():
            self.qr.setText("生成配对码后显示二维码")
            self.note.clear()
            return
        try:
            self.display_pairing_info(path)
        except Exception:
            self.qr.setText("配对信息暂时无法显示")
            self.note.clear()
            self.status.setText("无法读取已有配对信息，请检查文件或生成新的配对码。")

    def pair(self):
        try:
            self.display_pairing_info(issue_pairing_code(self.data_dir))
            self.status.setText("扫码后在手机上登记。添加下一台设备时，再生成一个新配对码。")
        except Exception:
            QMessageBox.warning(self, "未生成配对码", "无法生成配对码，请检查本机设备配置和依赖。")

    def revoke(self):
        row = self.table.currentRow()
        if row < 0:
            self.status.setText("请先选择需要撤销的设备。")
            return
        try:
            revoke_device(self.data_dir, self.table.item(row, 0).data(Qt.UserRole))
            self.refresh()
            self.status.setText("已撤销该设备。重新允许访问时，请生成新的配对码登记。")
        except Exception:
            QMessageBox.warning(self, "未撤销设备", "撤销未完成，请刷新列表后重试。")


class Devices(QDialog):
    """Compatibility window for the original standalone device manager."""

    def __init__(self, data_dir=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Claude Bridge · 手机与 iPad 设备")
        self.resize(760, 760)
        self.panel = DevicePanel(data_dir, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.panel)
        self.data_dir = self.panel.data_dir
        self.qr = self.panel.qr
        self.note = self.panel.note
        self.table = self.panel.table
        self.status = self.panel.status
        self.timer = self.panel.timer

    def pair(self):
        self.panel.pair()

    def refresh(self):
        self.panel.refresh()

    def revoke(self):
        self.panel.revoke()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=core.DATA_DIR)
    args = parser.parse_args()
    application = QApplication(sys.argv[:1])
    dialog = Devices(args.data_dir)
    dialog.show()
    raise SystemExit(application.exec())


if __name__ == "__main__":
    main()
