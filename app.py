from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSignalBlocker, QThreadPool, QTimer, Qt, Signal, QUrl
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter, QTabWidget, QToolButton,
    QVBoxLayout, QWidget, QStyle, QStyleOptionComboBox)

from core import (APP_DIR, DATA_DIR, PRESETS, BridgeError, CodexTranslator, SSH, Translator, history_record, load_config,
                  atomic_json, delivery_fingerprint, effective_reasoning_effort, make_profile, protect_key,
                  reasoning_options, save_config, send_fingerprint, unprotect_key, validate_text)
from media import build_bundle, check_cancel, compose_prompt, kind, upload_bundle
from reply_translations import ReplyCache, translation_key

STYLE = """
QWidget { color: #192E47; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMainWindow, QDialog { background: #EDF2F7; }
QLabel#brand { font-size: 26px; font-weight: 700; color: #173D68; }
QLabel#subtitle, QLabel#muted { color: #60758C; }
QLabel#section { font-size: 16px; font-weight: 600; }
QLabel#badge { background: #D6E5F7; color: #285B91; padding: 7px 12px; border-radius: 12px; }
QPlainTextEdit { background: white; border: 1px solid #CAD7E5; border-radius: 7px;
    padding: 12px; selection-background-color: #BCD7FA; }
QPlainTextEdit:focus { border: 1px solid #3577C6; }
QPlainTextEdit#terminal { background: #172F48; color: #D6E7F8; font-family: Consolas; font-size: 14px; }
QSplitter#conversationSplitter::handle:vertical { background: #D4DFEC; border-radius: 4px; margin: 3px 0; }
QSplitter#conversationSplitter::handle:vertical:hover { background: #83A6CD; }
QLineEdit, QComboBox { background: #FFFFFF; border: 1px solid #BDCCDE; border-radius: 5px;
    padding: 7px 9px; min-height: 20px; }
QComboBox::drop-down { width: 24px; border: 0; }
QPushButton { background: #FAFCFF; border: 1px solid #BCCBDE; border-radius: 6px;
    padding: 9px 15px; font-weight: 500; }
QPushButton:hover { background: #E4EEFB; border-color: #759BC9; }
QPushButton:pressed { background: #C9DFF8; }
QPushButton#primary { background: #2466AE; color: white; border-color: #2466AE; font-weight: 600; }
QPushButton#primary:hover { background: #1C5797; }
QPushButton#replyTranslate:checked { background: #2466AE; color: white; border-color: #2466AE; }
QPushButton:disabled { color: #9CACBD; background: #E5EBF2; border-color: #D0DAE5; }
QListWidget { background: transparent; border: 0; outline: 0; }
QListWidget::item { padding: 11px 7px; border-bottom: 1px solid #CDD9E6; }
QListWidget::item:selected { background: #C5D9F0; color: #173D68; border-radius: 4px; }
QTabWidget::pane { border: 0; padding: 12px 2px; }
QTabBar::tab { padding: 10px 22px; background: #DFE8F2; }
QTabBar::tab:selected { background: #FFFFFF; }
QCheckBox { spacing: 7px; }
QStatusBar { background: #DDE7F1; }
QMenuBar { background: #E2EAF3; padding: 2px 5px; }
QMenuBar::item { padding: 5px 12px; }
QMenuBar::item:selected, QMenu::item:selected { background: #C5D9F0; }
QMenu { background: #FAFCFF; border: 1px solid #BCCBDE; }
QMenu::item { padding: 8px 24px; }
"""

REPLY_READ_TIMEOUT = 60


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def confirmed_delivery(receipt, identity, enter):
    """A receipt query is evidence only; it must never resume a Send action."""
    if (not isinstance(receipt, dict) or receipt.get("found") is not True
            or receipt.get("matched") is not True or receipt.get("completed") is not True
            or receipt.get("fingerprint") != identity):
        return None
    if receipt.get("state") == "submitted":
        return "submitted"
    if enter is False and receipt.get("state") == "pasted" and receipt.get("auto_enter") is False:
        return "pasted"
    return None


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def menu_text(value):
    return str(value).replace("&", "&&")


def icon():
    image = QPixmap(64, 64)
    image.fill(QColor("#2466AE"))
    painter = QPainter(image)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Segoe UI", 26, QFont.Weight.Bold))
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "B")
    painter.end()
    return QIcon(image)


def attachment_icon():
    image = QPixmap(24, 24)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#285B91"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    from PySide6.QtGui import QPainterPath
    path = QPainterPath()
    path.moveTo(9, 13)
    path.lineTo(15, 7)
    path.cubicTo(18, 4, 22, 8, 19, 11)
    path.lineTo(10, 20)
    path.cubicTo(5, 25, -1, 18, 4, 13)
    path.lineTo(13, 4)
    path.cubicTo(18, -1, 25, 6, 20, 11)
    painter.translate(1, 0)
    painter.scale(.85, .85)
    painter.drawPath(path)
    painter.end()
    return QIcon(image)


class Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class PromptEdit(QPlainTextEdit):
    files_added = Signal(list)
    image_added = Signal(object)

    def canInsertFromMimeData(self, source):
        return source.hasUrls() or source.hasImage() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if self.isReadOnly():
            return
        if source.hasUrls():
            paths = [url.toLocalFile() for url in source.urls() if url.isLocalFile()]
            if paths:
                self.files_added.emit(paths)
                return
        if source.hasImage():
            value = source.imageData()
            image = value.toImage() if isinstance(value, QPixmap) else QImage(value)
            if not image.isNull():
                self.image_added.emit(image)
                return
        super().insertFromMimeData(source)


class Job(QRunnable):
    def __init__(self, work):
        super().__init__()
        self.work = work
        self.signals = Signals()

    def run(self):
        try:
            result = self.work()
        except Exception as exc:
            message = str(exc) if isinstance(exc, BridgeError) else f"操作失败（{type(exc).__name__}）。请检查配置。"
            self.signals.failed.emit(message)
        else:
            self.signals.done.emit(result)


class ModelSelector(QComboBox):
    """Keep the editable native list while making its dropdown affordance visible."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("modelSelector")
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(12)
        self.setAccessibleName("翻译模型")
        self.setToolTip("点击右侧箭头选择已读取的模型，也可直接输入模型名。")
        self.setStyleSheet("""
            QComboBox#modelSelector { padding-right: 36px; }
            QComboBox#modelSelector::drop-down { width: 32px; background: #EDF3FA;
                border-left: 1px solid #BDCCDE; border-top-right-radius: 4px; border-bottom-right-radius: 4px; }
            QComboBox#modelSelector::down-arrow { image: none; }
        """)

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        arrow = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                            QStyle.SubControl.SC_ComboBoxArrow, self)
        center = arrow.center()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#285B91" if self.isEnabled() else "#9CACBD"), 2,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawLine(center.x() - 5, center.y() - 2, center.x(), center.y() + 3)
        painter.drawLine(center.x(), center.y() + 3, center.x() + 5, center.y() - 2)
        painter.end()


class Settings(QDialog):
    def __init__(self, parent, section="api"):
        super().__init__(parent)
        self.setWindowTitle("Claude Bridge 设置")
        self.resize(760, 815)
        self.config = json.loads(json.dumps(parent.config))
        self.active = self.config["provider"]
        self.keys = {}
        self.jobs = []
        self.active_dialog = True
        self.finished.connect(self.deactivate)
        layout = QVBoxLayout(self)
        tabs = self.tabs = QTabWidget()
        layout.addWidget(tabs)
        api = QWidget()
        form = QFormLayout(api)
        form.setSpacing(14)
        self.provider = QComboBox()
        for pid, profile in self.config["profiles"].items():
            self.provider.addItem(profile.get("label", pid), pid)
        self.provider.setCurrentIndex(self.provider.findData(self.active))
        self.profile_name = QLineEdit()
        self.template = QComboBox()
        self.template.addItems(PRESETS)
        self.base = QLineEdit()
        self.protocol = QComboBox()
        self.protocol.addItem("Chat Completions", "chat")
        self.protocol.addItem("Responses（OpenAI）", "responses")
        self.codex_path = QLineEdit()
        self.codex_path.setPlaceholderText("留空自动查找本机 Codex CLI")
        self.model = ModelSelector()
        self.reasoning = ModelSelector()
        self.reasoning.setEditable(False)
        self.reasoning.setAccessibleName("翻译推理强度")
        self.reasoning.setToolTip("只影响 Bridge 的指令与回复翻译。普通翻译推荐 Low。")
        self.reasoning_preferred = ""
        self.reasoning_note = label("", "muted", True)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("填写 API Key，或留空使用环境变量")
        self.key_env = QLineEdit()
        self.key_env.setPlaceholderText("例如 OPENAI_API_KEY")
        form.addRow("已保存的连接", self.provider)
        profile_actions = QHBoxLayout()
        for title, callback in [("新增连接", lambda: self.add_profile(False)), ("复制连接", lambda: self.add_profile(True)),
                                ("删除连接", self.remove_profile)]:
            profile_actions.addWidget(button(title, callback))
        form.addRow("", profile_actions)
        form.addRow("连接名称", self.profile_name)
        form.addRow("服务类型", self.template)
        form.addRow("Base URL", self.base)
        form.addRow("API 协议", self.protocol)
        form.addRow("模型名", self.model)
        form.addRow("推理强度", self.reasoning)
        form.addRow("", self.reasoning_note)
        form.addRow("API Key", self.key)
        self.reveal = QCheckBox("显示 Key")
        self.reveal.toggled.connect(lambda yes: self.key.setEchoMode(QLineEdit.EchoMode.Normal if yes else QLineEdit.EchoMode.Password))
        form.addRow("", self.reveal)
        form.addRow("Key 环境变量", self.key_env)
        form.addRow("Codex CLI 路径", self.codex_path)
        self.models_button = button("读取模型", self.fetch_models)
        self.auth_button = button("检查 Codex 登录", self.check_codex)
        model_actions = QHBoxLayout()
        model_actions.addWidget(self.models_button)
        model_actions.addWidget(self.auth_button)
        form.addRow("", model_actions)
        openai_actions = QHBoxLayout()
        self.openai_buttons = []
        for title, url in [("OpenAI 额度账单", "https://platform.openai.com/settings/organization/billing/overview"),
                           ("API Key 管理", "https://platform.openai.com/api-keys"),
                           ("赠送额度说明", "https://developers.openai.com/api/docs/guides/chatgpt-api-credits")]:
            b = button(title, lambda checked=False, url=url: QDesktopServices.openUrl(QUrl(url)))
            self.openai_buttons.append(b)
            openai_actions.addWidget(b)
        form.addRow("", openai_actions)
        self.info = label("Key 使用 Windows 账户加密后保存在本机。API 仅用于翻译，Claude 使用 Ubuntu 上现有的登录与模型设置。", "muted", True)
        form.addRow(self.info)
        api_scroll = QScrollArea()
        api_scroll.setWidgetResizable(True)
        api_scroll.setFrameShape(QFrame.Shape.NoFrame)
        api_scroll.setWidget(api)
        tabs.addTab(api_scroll, "API / Codex 连接")
        ssh = QWidget()
        remote_form = QFormLayout(ssh)
        remote_form.setSpacing(14)
        self.remote_fields = {}
        for key, title in [("host", "SSH 别名 / user@host"), ("cwd", "Ubuntu 附件保存目录"),
                           ("desktop_bin", "Claude 桌面客户端启动路径")]:
            field = QLineEdit(self.config["ssh"].get(key, ""))
            self.remote_fields[key] = field
            remote_form.addRow(title, field)
        remote_form.addRow(label("发送到 Ubuntu 当前打开的 Claude 桌面对话，沿用客户端里的账户和模型。端口、私钥等沿用 Windows 的 ~/.ssh/config。", "muted", True))
        remote_form.addRow(label("首次使用：保存设置 → 配置 Ubuntu → 打开客户端。请在 Ubuntu 完成登录；发送前让目标对话保持空白输入框。", "muted", True))
        tabs.addTab(ssh, "Ubuntu / SSH")
        from mobile_devices import DevicePanel
        self.devices = DevicePanel(DATA_DIR, self)
        tabs.addTab(self.devices, "手机 / iPad 配对")
        tabs.setCurrentIndex({"api": 0, "ssh": 1, "devices": 2}.get(section, 0))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存设置")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load_profile()
        self.provider.currentIndexChanged.connect(self.switch)
        self.template.currentTextChanged.connect(self.template_changed)
        self.model.currentTextChanged.connect(lambda _text: self.refresh_reasoning())
        self.base.textChanged.connect(lambda _text: self.refresh_reasoning())
        self.protocol.currentIndexChanged.connect(lambda _index: self.refresh_reasoning())

    def deactivate(self, _result):
        self.active_dialog = False

    def finish_job(self, job):
        if job in self.jobs:
            self.jobs.remove(job)
        return self.active_dialog

    def refill_profiles(self):
        with QSignalBlocker(self.provider):
            self.provider.clear()
            for pid, profile in self.config["profiles"].items():
                self.provider.addItem(profile.get("label", pid), pid)
            self.provider.setCurrentIndex(self.provider.findData(self.active))

    def add_profile(self, duplicate=False):
        self.collect_profile()
        original = self.config["profiles"][self.active]
        pid = "connection-" + uuid.uuid4().hex[:12]
        base_label = original["label"] + (" 副本" if duplicate else " 新连接")
        profile = json.loads(json.dumps(original)) if duplicate else make_profile(original["template"])
        existing = {p.get("label") for p in self.config["profiles"].values()}
        title, n = base_label, 2
        while title in existing:
            title, n = f"{base_label} {n}", n + 1
        profile["label"] = title
        self.config["profiles"][pid] = profile
        self.keys[pid] = self.keys.get(self.active, "") if duplicate else ""
        self.active = pid
        self.refill_profiles()
        self.load_profile()
        self.profile_name.setFocus()
        self.profile_name.selectAll()

    def remove_profile(self):
        if len(self.config["profiles"]) <= 1:
            self.info.setText("请至少保留一个连接。")
            return
        title = self.config["profiles"][self.active].get("label", self.active)
        if QMessageBox.question(self, "删除连接", f"删除“{title}”的本机配置？其他连接和原始 Key 来源文件会保留。") != QMessageBox.StandardButton.Yes:
            return
        del self.config["profiles"][self.active]
        self.keys.pop(self.active, None)
        self.active = next(iter(self.config["profiles"]))
        self.refill_profiles()
        self.load_profile()

    def template_changed(self, template):
        name = self.profile_name.text().strip()
        self.config["profiles"][self.active] = make_profile(template, name)
        self.keys[self.active] = ""
        self.load_profile()

    def load_profile(self):
        self.model_profile_revision = getattr(self, "model_profile_revision", 0) + 1
        p = self.config["profiles"][self.active]
        self.profile_name.setText(p.get("label", self.active))
        with QSignalBlocker(self.template):
            self.template.setCurrentText(p.get("template", "自定义兼容 API"))
        self.base.setText(p.get("base_url", ""))
        self.protocol.setCurrentIndex(max(0, self.protocol.findData(p.get("protocol", "chat"))))
        self.codex_path.setText(p.get("codex_path", ""))
        with QSignalBlocker(self.model):
            self.model.clear()
            self.model.addItems(p.get("known_models", []))
            self.model.setCurrentText(p.get("model", ""))
        self.key_env.setText(p.get("key_env", ""))
        if self.active not in self.keys:
            try:
                self.keys[self.active] = unprotect_key(p.get("key_encrypted", ""))
            except BridgeError:
                self.keys[self.active] = ""
                self.info.setText("已保存的 Key 无法解密，请重新填写。")
        self.key.setText(self.keys[self.active])
        self.reveal.setChecked(False)
        codex = p.get("kind") == "codex"
        for field in [self.base, self.protocol, self.key, self.key_env, self.reveal]:
            field.setEnabled(not codex)
        self.codex_path.setEnabled(codex)
        self.auth_button.setEnabled(codex)
        self.models_button.setText("读取本机模型" if codex else "读取模型")
        self.model.setPlaceholderText("留空使用 Codex 默认模型" if codex else "填写模型名或读取模型")
        for b in self.openai_buttons:
            b.setVisible(p.get("template", "").startswith("OpenAI"))
        self.info.setText("使用本机 ChatGPT 登录的 Codex 额度进行翻译，无需 API Key。额度受账户当前限制；Ubuntu 上仍由 Claude 执行。" if codex else
                          "API Key 使用 Windows 账户加密后保存。可创建多组同一服务商连接。OpenAI 赠送和付费余额以平台组织账单为准。")
        try:
            selected_effort = effective_reasoning_effort(p)
        except BridgeError:
            selected_effort = p.get("reasoning_effort", "")
        self.refresh_reasoning(selected_effort)

    def refresh_reasoning(self, preferred=None):
        profile = {**self.config["profiles"][self.active], "model": self.model.currentText().strip(),
                   "base_url": self.base.text().strip(), "protocol": self.protocol.currentData()}
        if preferred is not None:
            self.reasoning_preferred = preferred
        elif self.reasoning.isEnabled() and self.reasoning.currentData() is not None:
            self.reasoning_preferred = self.reasoning.currentData()
        selected = self.reasoning_preferred
        options = reasoning_options(profile)
        titles = {"none": "关闭思考 · None", "minimal": "最少 · Minimal", "low": "低 · Low（翻译推荐）",
                  "medium": "中 · Medium", "high": "高 · High", "xhigh": "超高 · Extra high", "max": "最大 · Max"}
        with QSignalBlocker(self.reasoning):
            self.reasoning.clear()
            self.reasoning.addItem("自动（使用模型默认值）", "")
            for value in options:
                self.reasoning.addItem(titles.get(value, value), value)
            self.reasoning.setCurrentIndex(max(0, self.reasoning.findData(selected)))
        self.reasoning.setEnabled(bool(options))
        self.reasoning_note.setText(
            "普通翻译推荐 Low。Ultra 属于多代理模式，本工具保持单模型翻译。" if options and profile.get("kind") == "codex" else
            "普通翻译推荐 Low；复杂语义可提高档位。自动按服务商默认值执行。" if options else
            "此模型未提供可选推理档位，使用模型默认设置。")

    def collect_profile(self):
        p = self.config["profiles"][self.active]
        p.update(base_url=self.base.text().strip(), model=self.model.currentText().strip(), key_env=self.key_env.text().strip(),
                 label=self.profile_name.text().strip() or self.active, codex_path=self.codex_path.text().strip(),
                 protocol=self.protocol.currentData(),
                 reasoning_effort=self.reasoning.currentData() or "",
                 known_models=[self.model.itemText(i) for i in range(self.model.count())])
        self.keys[self.active] = self.key.text().strip()
        self.provider.setItemText(self.provider.findData(self.active), p["label"])

    def switch(self, index):
        name = self.provider.itemData(index)
        if not name or name == self.active:
            return
        self.collect_profile()
        self.active = name
        self.load_profile()

    def fetch_models(self):
        import hashlib

        self.collect_profile()
        p = dict(self.config["profiles"][self.active])
        key = self.key.text().strip() or os.environ.get(p["key_env"], "")

        def source_identity():
            profile = self.config["profiles"].get(self.active, {})
            key_env = self.key_env.text().strip()
            current_key = self.key.text().strip() or os.environ.get(key_env, "")
            return (self.active, profile.get("kind", "api"), self.template.currentText(),
                    self.base.text().strip(), self.protocol.currentData(), self.codex_path.text().strip(),
                    key_env, hashlib.sha256(current_key.encode()).digest())

        origin = source_identity()
        revision = self.model_profile_revision
        self.models_button.setEnabled(False)
        self.provider.setEnabled(False)
        self.info.setText("正在读取可用模型…")
        job = Job(lambda: (CodexTranslator(p) if p.get("kind") == "codex" else Translator(p, key)).models())
        self.jobs.append(job)

        def finish_read():
            if not self.finish_job(job):
                return False
            self.models_button.setEnabled(True)
            self.provider.setEnabled(True)
            if revision != self.model_profile_revision or origin != source_identity():
                self.info.setText("连接设置已变化，已忽略旧模型列表。请重新读取模型。")
                return False
            return True

        def finished(models):
            if not finish_read():
                return
            old = self.model.currentText()
            effort = self.reasoning.currentData()
            with QSignalBlocker(self.model):
                self.model.clear()
                self.model.addItems(models)
                self.model.setCurrentText(old or (models[0] if models and p.get("kind") != "codex" else ""))
            self.refresh_reasoning(effort)
            self.info.setText(f"读取到 {len(models)} 个模型，可选择或手工填写模型名。")
        def failed(message):
            if not finish_read():
                return
            self.info.setText(message)
        job.signals.done.connect(finished)
        job.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(job)

    def check_codex(self):
        self.collect_profile()
        p = dict(self.config["profiles"][self.active])
        self.auth_button.setEnabled(False)
        self.info.setText("正在检查本机 Codex 登录…")
        job = Job(lambda: CodexTranslator(p).status())
        self.jobs.append(job)
        def finished(result):
            if not self.finish_job(job):
                return
            self.auth_button.setEnabled(True)
            self.info.setText(result["text"] + " 实际剩余额度以 Codex 为准。")
        def failed(message):
            if not self.finish_job(job):
                return
            self.auth_button.setEnabled(True)
            self.info.setText(message)
        job.signals.done.connect(finished)
        job.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(job)

    def save(self):
        try:
            self.collect_profile()
            for name, key in self.keys.items():
                self.config["profiles"][name]["key_encrypted"] = protect_key(key)
            self.config["provider"] = self.active
            self.config["ssh"].update({k: f.text().strip() for k, f in self.remote_fields.items()})
            self.config["ssh"]["transport"] = "desktop"
            SSH(self.config["ssh"])
            save_config(self.config)
        except BridgeError as exc:
            QMessageBox.warning(self, "设置未保存", str(exc))
            return
        self.accept()


class ProjectPicker(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.bridge_window = parent
        self.target = dict(parent.config["ssh"])
        self.catalog = None
        self.jobs = []
        self.loading = False
        self.active_dialog = True
        self.finished.connect(self.deactivate)
        self.setWindowTitle("切换 Ubuntu Code 项目")
        self.resize(720, 570)
        layout = QVBoxLayout(self)
        layout.addWidget(label("选择项目后，直接显示该项目最近使用的会话。也可选择其他会话后点击“进入会话”。", "section", True))
        form = QFormLayout()
        self.project_combo = QComboBox()
        self.project_combo.setAccessibleName("Code 项目")
        self.project_combo.currentIndexChanged.connect(self.project_changed)
        form.addRow("项目", self.project_combo)
        layout.addLayout(form)
        self.session_list = QListWidget()
        self.session_list.setAccessibleName("Code 会话")
        self.session_list.currentItemChanged.connect(lambda *_: self.update_controls())
        layout.addWidget(self.session_list, 1)
        self.status = label("正在读取 Ubuntu 项目…", "muted", True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.refresh_button = button("刷新项目", self.refresh_catalog)
        self.switch_button = button("进入会话", self.switch_session, True)
        close_button = button("关闭", self.reject)
        actions.addWidget(self.refresh_button)
        actions.addStretch()
        actions.addWidget(self.switch_button)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        self.update_controls()
        QTimer.singleShot(0, self.refresh_catalog)

    def deactivate(self, _result):
        self.active_dialog = False

    def update_controls(self):
        if not self.active_dialog:
            return
        enabled = not self.loading and not self.bridge_window.busy
        current_target = self.target == self.bridge_window.config["ssh"]
        self.refresh_button.setEnabled(enabled)
        self.project_combo.setEnabled(enabled and current_target and self.catalog is not None)
        self.session_list.setEnabled(enabled and current_target)
        selected = self.session_list.currentItem()
        session = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.switch_button.setEnabled(bool(enabled and current_target and session and session.get("selectable")
                                           and self.catalog and self.catalog.get("session_catalog_id")))
        if not current_target and not self.loading:
            self.status.setText("SSH 设置已变化，请刷新项目列表。")

    def read_job(self, message, work):
        if not self.active_dialog or self.loading or self.bridge_window.busy:
            return
        if self.target != self.bridge_window.config["ssh"]:
            self.update_controls()
            return
        self.loading = True
        self.status.setText(message)
        self.update_controls()
        job = Job(work)
        self.jobs.append(job)

        def done(result):
            if job in self.jobs:
                self.jobs.remove(job)
            self.loading = False
            if not self.active_dialog:
                return
            self.show_catalog(result)

        def failed(message):
            if job in self.jobs:
                self.jobs.remove(job)
            self.loading = False
            if not self.active_dialog:
                return
            self.status.setText(message)
            self.update_controls()

        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(job)

    def refresh_catalog(self):
        if not self.active_dialog or self.loading or self.bridge_window.busy:
            return
        self.target = dict(self.bridge_window.config["ssh"])
        self.read_job("正在读取 Ubuntu 项目…", lambda: SSH(self.target).action("list_projects", timeout=60))

    def show_catalog(self, result):
        if not isinstance(result.get("projects"), list) or not result.get("context") or not result.get("catalog_id"):
            self.status.setText("项目列表未返回完整信息，请刷新后重试。")
            self.update_controls()
            return
        selected = result.get("selected_project_id", "") if result.get("scope") == "sessions" else ""
        self.catalog = result
        with QSignalBlocker(self.project_combo):
            self.project_combo.clear()
            self.project_combo.addItem("选择项目", "")
            for project in result["projects"]:
                title = project["title"] + ("（当前）" if project.get("active") else "")
                self.project_combo.addItem(title, project["id"])
                item = self.project_combo.model().item(self.project_combo.count() - 1)
                item.setEnabled(bool(project.get("selectable")))
                item.setToolTip(project.get("reason", ""))
            index = self.project_combo.findData(selected)
            self.project_combo.setCurrentIndex(index if index >= 0 else 0)
        self.fill_sessions()

    def selected_project(self):
        if self.catalog is None:
            return None
        selected = self.project_combo.currentData()
        matches = [project for project in self.catalog["projects"] if project["id"] == selected]
        return matches[0] if len(matches) == 1 else None

    def fill_sessions(self):
        self.session_list.clear()
        project = self.selected_project()
        if project is None:
            self.status.setText("请选择项目。勾选标记表示 Ubuntu 当前会话。")
        elif (self.catalog.get("scope") == "sessions" and project.get("sessions_loaded") is True
              and self.catalog.get("selected_project_id") == project["id"]):
            sessions = project.get("sessions", [])
            for session in sessions:
                item = QListWidgetItem(session["title"] + ("（当前）" if session.get("active") else ""))
                item.setData(Qt.ItemDataRole.UserRole, session)
                item.setCheckState(Qt.CheckState.Checked if session.get("active") else Qt.CheckState.Unchecked)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled if session.get("selectable") else Qt.ItemFlag.NoItemFlags)
                item.setToolTip(session.get("reason", ""))
                self.session_list.addItem(item)
            self.status.setText("最近会话已打开，回复显示在主窗口。可选择其他会话后点击“进入会话”。" if sessions else
                                "这个项目暂无可见会话。请在 Ubuntu 创建会话后刷新。")
        else:
            self.status.setText("请选择项目以读取它的会话。")
        self.session_list.setCurrentRow(-1)
        self.session_list.clearSelection()
        self.update_controls()

    def project_changed(self, _index):
        if not self.active_dialog or self.loading or self.bridge_window.busy:
            return
        if self.target != self.bridge_window.config["ssh"]:
            self.update_controls()
            return
        project = self.selected_project()
        self.session_list.clear()
        if project and project.get("selectable"):
            self.navigate("open_project", self.selection_payload(project["id"]), close=False)
        else:
            self.fill_sessions()

    def selection_payload(self, project_id, session_id=""):
        payload = {"context": self.catalog["context"], "catalog_id": self.catalog["catalog_id"], "project_id": project_id}
        if session_id:
            payload.update(session_id=session_id, session_catalog_id=self.catalog.get("session_catalog_id", ""))
        return payload

    def switch_session(self):
        if not self.active_dialog or self.loading or self.bridge_window.busy:
            return
        if self.target != self.bridge_window.config["ssh"]:
            self.update_controls()
            return
        project = self.selected_project()
        selected = self.session_list.currentItem()
        session = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        if project is None or not project.get("selectable") or not session or not session.get("selectable"):
            return
        if not self.catalog.get("session_catalog_id") or self.catalog.get("selected_project_id") != project["id"]:
            self.status.setText("会话列表已失效，请重新选择项目。")
            return
        payload = self.selection_payload(project["id"], session["id"])
        self.navigate("switch_project", payload, close=True)

    def navigate(self, action, payload, *, close):
        self.loading = True
        self.status.setText("正在打开项目最近会话…" if action == "open_project" else "正在切换 Ubuntu 会话…")
        self.update_controls()
        target = dict(self.target)
        self.bridge_window.pending_reply_context = ""
        self.bridge_window.reply_original = ""
        self.bridge_window.reply_code_blocks = []
        self.bridge_window.reply_generating = False
        self.bridge_window.render_reply()
        self.bridge_window.terminal.setPlaceholderText("正在读取所选项目的最近对话…")

        def done(result):
            self.loading = False
            self.bridge_window.project_switched(result)
            if self.active_dialog:
                self.show_catalog(result)
                if close:
                    self.accept()

        def failed(message):
            self.loading = False
            if self.active_dialog:
                self.status.setText(message)
                self.update_controls()
            self.bridge_window.pending_reply_context = ""
            self.bridge_window.refresh(quiet=True)

        self.bridge_window.job("正在切换项目…", lambda: SSH(target).action(action, timeout=110, **payload),
                               done, failed, quiet=True)


class Window(QMainWindow):
    transfer_progress = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Claude Bridge · 中文指令直达 Ubuntu")
        self.setWindowIcon(icon())
        self.resize(1420, 940)
        self.setMinimumSize(1080, 540)
        self.config = load_config()
        self.connection_configured = (DATA_DIR / "config.json").is_file()
        self.busy = False
        self.jobs = []
        self.draft_source = None
        self.last_fingerprint = None
        self.pending_deliveries = {}
        self.resolved_deliveries = set()
        self.delivery_job = None
        self.delivery_cursor = 0
        self.history = []
        self.history_dialog = None
        self.history_list = None
        self.history_action = None
        self.history_actions = []
        self.history_empty_action = None
        self.project_dialog = None
        self.reply_original = ""
        self.reply_code_blocks = []
        self.reply_generating = False
        self.reply_read_status = ""
        self.reply_read_error = ""
        self.reply_job = None
        self.capture_job = None
        self.capture_epoch = 0
        self.capture_pending = False
        self.reply_context = ""
        self.pending_reply_context = ""
        self.current_target_title = ""
        self.current_target_host = ""
        self.current_target_context = ""
        self.closing = False
        self.reply_cache = ReplyCache(DATA_DIR / "reply-translations")
        self.reply_failures = {}
        self.attachments = []
        self.cancel_transfer = threading.Event()
        self.transfer_lock = threading.Lock()
        self.transfer_stage = "idle"
        self.transfer_progress.connect(self.show_transfer_progress)
        self.controls = []
        self.settings_menu = self.menuBar().addMenu("设置")
        self.settings_actions = {}
        for section, title in [("api", "API / Codex 连接"), ("ssh", "Ubuntu / SSH"),
                               ("devices", "手机 / iPad 配对")]:
            action = self.settings_menu.addAction(title)
            action.triggered.connect(lambda checked=False, section=section: self.settings(section))
            self.settings_actions[section] = action
        self.connection_menu = self.menuBar().addMenu("翻译连接")
        self.connection_status_action = self.connection_menu.addAction("")
        self.connection_status_action.setEnabled(False)
        self.connection_menu.addSeparator()
        self.connection_group = QActionGroup(self)
        self.connection_group.setExclusive(True)
        self.connection_actions = {}
        self.connection_separator = self.connection_menu.addSeparator()
        self.manage_connections_action = self.connection_menu.addAction("管理 API / Codex 连接…")
        self.manage_connections_action.triggered.connect(lambda: self.settings("api"))
        self.ubuntu_menu = self.menuBar().addMenu("Ubuntu 客户端")
        self.target_action = self.ubuntu_menu.addAction("")
        self.target_action.setEnabled(False)
        self.ubuntu_menu.addSeparator()
        self.ubuntu_actions = {}
        for key, text, callback in [("match", "匹配当前对话", self.match_current),
                                    ("setup", "配置 Ubuntu", self.setup_remote), ("probe", "测试 SSH 连接", self.probe),
                                    ("start", "打开 / 显示客户端", self.start), ("new", "新建对话", self.new_session),
                                    ("projects", "切换项目…", self.pick_project),
                                    ("settings", "Ubuntu / SSH 设置…", lambda: self.settings("ssh"))]:
            action = self.ubuntu_menu.addAction(text)
            action.triggered.connect(callback)
            self.ubuntu_actions[key] = action
        self.history_menu = self.menuBar().addMenu("发送记录")
        self.history_action = self.history_menu.addAction("查看发送记录…")
        self.history_action.triggered.connect(self.show_history_dialog)
        self.history_menu.addSeparator()
        self.history_menu.aboutToShow.connect(self.load_history)
        root = QWidget()
        self.setCentralWidget(root)
        content = QVBoxLayout(root)
        content.setContentsMargins(28, 27, 28, 18)
        content.setSpacing(16)
        header = QHBoxLayout()
        title_area = QVBoxLayout()
        title_area.addWidget(label("把想法交给 Claude", "brand"))
        title_area.addWidget(label("中文指令 → 英文翻译 → Ubuntu Claude 桌面客户端", "subtitle"))
        header.addLayout(title_area)
        header.addStretch()
        self.badge = label("准备就绪", "badge")
        header.addWidget(self.badge)
        content.addLayout(header)
        self.body_splitter = QSplitter(Qt.Orientation.Vertical)
        self.body_splitter.setObjectName("conversationSplitter")
        self.body_splitter.setHandleWidth(12)
        compose_area = QWidget()
        compose = QVBoxLayout(compose_area)
        compose.setContentsMargins(0, 0, 0, 0)
        compose.setSpacing(10)
        self.body_splitter.addWidget(compose_area)
        reply_area = QWidget()
        reply = QVBoxLayout(reply_area)
        reply.setContentsMargins(0, 0, 0, 0)
        reply.setSpacing(8)
        self.body_splitter.addWidget(reply_area)
        self.body_splitter.setCollapsible(0, True)
        self.body_splitter.setCollapsible(1, False)
        content.addWidget(self.body_splitter, 1)
        editors = QSplitter(Qt.Orientation.Horizontal)
        for title, placeholder, english in [
            ("你的中文指令", "例如：检查这个项目的启动报错，保留现有功能，修复后说明修改和验证结果。", False),
            ("交给 Claude 的英文稿", "翻译结果会显示在这里，也可以直接编辑英文稿。", True),
        ]:
            pane = QWidget()
            box = QVBoxLayout(pane)
            box.setContentsMargins(0, 0, 0, 0)
            editor_header = QHBoxLayout()
            editor_header.addWidget(label(title, "section"))
            editor_header.addStretch()
            if not english:
                self.attach_button = QToolButton()
                self.attach_button.setIcon(attachment_icon())
                self.attach_button.setToolTip("载入附件，也可直接拖入图片、视频或文件")
                self.attach_button.setAccessibleName("载入附件")
                self.attach_button.setFixedSize(30, 28)
                self.attach_button.setAutoRaise(True)
                self.attach_button.clicked.connect(self.choose_attachments)
                editor_header.addWidget(self.attach_button)
                self.controls.append(self.attach_button)
            box.addLayout(editor_header)
            edit = PromptEdit()
            edit.setPlaceholderText(placeholder)
            edit.setTabStopDistance(32)
            edit.setFont(QFont("Microsoft YaHei UI", 11))
            edit.files_added.connect(self.add_attachments)
            edit.image_added.connect(self.add_clipboard_image)
            box.addWidget(edit)
            if english:
                self.english = edit
                self.english.textChanged.connect(self.english_changed)
            else:
                self.source = edit
                self.source.textChanged.connect(self.source_changed)
            editors.addWidget(pane)
        editors.setSizes([520, 520])
        editors.setHandleWidth(18)
        editors.setMinimumHeight(110)
        compose.addWidget(editors, 1)
        self.note = label("可先检查英文稿，也可一键翻译并发送。代码块与内联代码会原样保留。", "muted", True)
        compose.addWidget(self.note)
        self.media_previews = QCheckBox("生成图片 / 视频预览")
        self.media_previews.setChecked(True)
        self.media_previews.setVisible(False)
        self.attachment_list = QListWidget()
        self.attachment_list.setStyleSheet("QListWidget::item { padding: 0; border-bottom: 1px solid #CDDAE8; }")
        self.attachment_list.setMaximumHeight(70)
        self.attachment_list.setMinimumHeight(42)
        self.attachment_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.attachment_list.setVisible(False)
        compose.addWidget(self.attachment_list)
        self.attachment_hint = label("拖入图片、视频或文件，Ctrl+V 粘贴截图。视频以抽帧预览和元数据载入 Claude。", "muted", True)
        compose.addWidget(self.attachment_hint)
        transfer_bar = QHBoxLayout()
        self.upload_status = label("", "muted", True)
        transfer_bar.addWidget(self.upload_status, 1)
        self.cancel_upload_button = button("停止上传", self.stop_upload)
        self.cancel_upload_button.setEnabled(False)
        self.cancel_upload_button.setVisible(False)
        transfer_bar.addWidget(self.cancel_upload_button)
        compose.addLayout(transfer_bar)
        actions = QHBoxLayout()
        self.translate_button = button("仅翻译", lambda: self.translate(False))
        self.send_button = button("发送英文稿", self.send)
        self.both_button = button("翻译并发送", lambda: self.translate(True), True)
        for b in [self.translate_button, self.send_button, self.both_button]:
            actions.addWidget(b)
            self.controls.append(b)
        actions.addStretch()
        self.enter = QCheckBox("载入后自动发送")
        self.enter.setChecked(self.config.get("auto_enter", True))
        self.enter.toggled.connect(self.enter_changed)
        actions.addWidget(self.enter)
        compose.addLayout(actions)
        terminal_header = QHBoxLayout()
        self.reply_heading = label("Claude 回复", "section")
        terminal_header.addWidget(self.reply_heading)
        self.reply_translation_status = label("", "muted")
        terminal_header.addWidget(self.reply_translation_status)
        terminal_header.addStretch()
        self.reply_translate = QPushButton("自动翻译")
        self.reply_translate.setObjectName("replyTranslate")
        self.reply_translate.setCheckable(True)
        self.reply_translate.setChecked(bool(self.config.get("auto_translate_reply", False)))
        self.reply_translate.setToolTip("激活后自动译成中文；关闭显示原文。沿用顶部菜单中选择的翻译连接，代码和链接保留原样。")
        self.reply_translate.toggled.connect(self.reply_translation_toggled)
        terminal_header.addWidget(self.reply_translate)
        self.auto_refresh = QCheckBox("自动刷新")
        terminal_header.addWidget(self.auto_refresh)
        self.refresh_button = button("刷新", self.refresh)
        terminal_header.addWidget(self.refresh_button)
        reply.addLayout(terminal_header)
        self.terminal = QPlainTextEdit()
        self.terminal.setObjectName("terminal")
        self.terminal.setReadOnly(True)
        self.terminal.setPlaceholderText("当前对话的最新回复会显示在这里。拖动上方分隔条调整阅读区高度。")
        self.terminal.setMinimumHeight(50)
        reply.addWidget(self.terminal, 1)
        self.controls += [self.refresh_button, self.enter, self.attachment_list]
        self.statusBar().showMessage("Ctrl+Enter 翻译并发送    |    Ctrl+T 仅翻译")
        self.shortcut_send = QShortcut(QKeySequence("Ctrl+Return"), self, activated=lambda: self.translate(True) if not self.busy else None)
        self.shortcut_translate = QShortcut(QKeySequence("Ctrl+T"), self, activated=lambda: self.translate(False) if not self.busy else None)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.refresh(quiet=True) if self.auto_refresh.isChecked() and not self.busy else None)
        self.timer.start(5000)
        self.update_summary()
        self.load_history()
        self.setAcceptDrops(True)
        self.draft_timer = QTimer(self)
        self.draft_timer.setSingleShot(True)
        self.draft_timer.timeout.connect(self.save_draft)
        self.source.textChanged.connect(lambda: self.draft_timer.start(500))
        self.english.textChanged.connect(lambda: self.draft_timer.start(500))
        self.restore_draft()
        if not self.connection_configured:
            self.note.setText("首次使用：请在「设置」中填写自己的翻译连接和 Ubuntu SSH 连接，再保存设置。")
            self.badge.setText("请先设置连接")
        self.delivery_timer = QTimer(self)
        self.delivery_timer.timeout.connect(self.poll_deliveries)
        self.delivery_timer.start(10000)
        sizes = self.config.get("pane_sizes", [460, 280])
        if not isinstance(sizes, list) or len(sizes) != 2 or not all(isinstance(x, int) and 0 <= x <= 10000 for x in sizes):
            sizes = [460, 280]
        self.body_splitter.setSizes(sizes)
        self.body_splitter.handle(1).setToolTip("上下拖动，调整输入区和回复区的高度")
        self.body_splitter.handle(1).setAccessibleName("调整回复区域高度")
        self.layout_timer = QTimer(self)
        self.layout_timer.setSingleShot(True)
        self.layout_timer.timeout.connect(self.save_layout)
        self.body_splitter.splitterMoved.connect(lambda *_: self.layout_timer.start(250))
        if self.reply_translate.isChecked():
            self.auto_refresh.setChecked(True)
            QTimer.singleShot(0, lambda: self.refresh(quiet=True) if self.reply_translate.isChecked() and not self.busy else None)

    def save_layout(self):
        self.config["pane_sizes"] = self.body_splitter.sizes()
        save_config(self.config)

    def dragEnterEvent(self, event):
        if not self.busy and event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not self.busy:
            self.add_attachments([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
            event.acceptProposedAction()

    def choose_attachments(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加附件", "", "媒体和文件 (*.*)")
        if paths:
            self.add_attachments(paths)

    def add_attachments(self, paths):
        if self.busy:
            return
        existing = {str(Path(p).resolve()) for p in self.attachments}
        failures = []
        for value in paths:
            path = Path(value).resolve()
            if not path.is_file():
                failures.append(path.name + "：不是普通文件")
            elif str(path) not in existing:
                if len(self.attachments) >= 64:
                    failures.append("每条指令最多添加 64 个附件")
                    break
                self.attachments.append(str(path))
                existing.add(str(path))
        self.refresh_attachment_list()
        if failures:
            QMessageBox.warning(self, "部分附件未添加", "\n".join(failures))

    def add_clipboard_image(self, image):
        if self.busy or image.isNull():
            return
        directory = DATA_DIR / "clipboard"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("Screenshot-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".png")
        if not image.save(str(path), "PNG"):
            QMessageBox.warning(self, "未添加截图", "截图保存失败。")
            return
        self.add_attachments([str(path)])

    def paste_image(self):
        clipboard = QApplication.clipboard()
        image = clipboard.image()
        if not image.isNull():
            self.add_clipboard_image(image)
        elif clipboard.mimeData().hasUrls():
            self.add_attachments([url.toLocalFile() for url in clipboard.mimeData().urls() if url.isLocalFile()])
        else:
            self.statusBar().showMessage("剪贴板中没有图片或文件。")

    def remove_attachment(self):
        rows = {self.attachment_list.row(item) for item in self.attachment_list.selectedItems()}
        self.attachments = [path for i, path in enumerate(self.attachments) if i not in rows]
        self.refresh_attachment_list()

    def clear_attachments(self):
        self.attachments = []
        self.refresh_attachment_list()

    def remove_attachment_at(self, path):
        if not self.busy:
            self.attachments = [p for p in self.attachments if p != path]
            self.refresh_attachment_list()

    def refresh_attachment_list(self):
        self.attachment_list.clear()
        total = 0
        names = {"image": "图片", "video": "视频", "audio": "音频", "file": "文件"}
        for value in self.attachments:
            path = Path(value)
            size = path.stat().st_size if path.is_file() else 0
            total += size
            item = QListWidgetItem()
            item.setToolTip(str(path))
            self.attachment_list.addItem(item)
            row = QWidget()
            row.setMinimumHeight(30)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 1, 4, 1)
            row_layout.addWidget(label(f"{names[kind(path)]}  {path.name}    {size/1024/1024:.2f} MB" + ("  [文件缺失]" if not path.is_file() else "")), 1)
            remove = QToolButton()
            remove.setText("×")
            remove.setAutoRaise(True)
            remove.setToolTip("移除此附件")
            remove.setAccessibleName("移除附件 " + path.name)
            remove.clicked.connect(lambda checked=False, p=value: self.remove_attachment_at(p))
            row_layout.addWidget(remove)
            item.setSizeHint(row.sizeHint())
            self.attachment_list.setItemWidget(item, row)
        self.attachment_list.setVisible(bool(self.attachments))
        self.attachment_hint.setText(f"{len(self.attachments)} 个附件 · {total/1024/1024:.2f} MB · 视频以抽帧预览载入，原件保存在 Ubuntu。" if self.attachments else
                                     "拖入图片、视频或文件，Ctrl+V 粘贴截图。视频以抽帧预览和元数据载入 Claude。")
        if hasattr(self, "draft_timer"):
            self.draft_timer.start(500)

    def save_draft(self):
        try:
            atomic_json(DATA_DIR / "draft.json", {"source": self.source.toPlainText(), "english": self.english.toPlainText(),
                "draft_source": self.draft_source, "attachments": self.attachments})
        except OSError:
            self.statusBar().showMessage("本机草稿保存失败，请检查数据目录。")

    def restore_draft(self):
        try:
            path = DATA_DIR / "draft.json"
            if not path.exists():
                return
            draft = json.loads(path.read_text(encoding="utf-8"))
            self.source.setPlainText(draft.get("source", ""))
            self.english.setPlainText(draft.get("english", ""))
            self.draft_source = draft.get("draft_source")
            self.attachments = [p for p in draft.get("attachments", []) if isinstance(p, str)]
            self.refresh_attachment_list()
            if self.english.toPlainText().strip() and self.draft_source != self.source.toPlainText():
                self.source_changed()
            else:
                self.note.setText("已恢复上次草稿。可继续编辑，或直接发送已有英文稿。" if self.english.toPlainText().strip() else
                                  "已恢复上次中文草稿。可先翻译检查英文稿，或一键翻译并发送。")
        except (OSError, ValueError, TypeError):
            self.statusBar().showMessage("本机草稿无法恢复，原文件已保留。")

    def clear_submitted_draft(self):
        with QSignalBlocker(self.source), QSignalBlocker(self.english):
            self.source.clear()
            self.english.clear()
        self.draft_source = None
        self.attachments = []
        self.refresh_attachment_list()
        self.draft_timer.stop()
        self.save_draft()
        self.source.setFocus()

    def show_transfer_progress(self, message):
        self.upload_status.setText(message[:110])
        self.statusBar().showMessage(message)
        if self.transfer_stage != "upload":
            self.cancel_upload_button.setEnabled(False)

    def stop_upload(self):
        with self.transfer_lock:
            if self.transfer_stage != "upload":
                self.cancel_upload_button.setEnabled(False)
                self.upload_status.setText("上传已完成，正在确认指令送达。")
                return
            self.cancel_transfer.set()
            self.cancel_upload_button.setEnabled(False)
            self.upload_status.setText("正在停止上传；指令尚未发送…")

    def update_summary(self):
        provider = self.config["provider"]
        p = self.config["profiles"][provider]
        for pid in list(self.connection_actions):
            if pid not in self.config["profiles"]:
                action = self.connection_actions.pop(pid)
                self.connection_menu.removeAction(action)
                self.connection_group.removeAction(action)
                action.deleteLater()
        for pid, profile in self.config["profiles"].items():
            action = self.connection_actions.get(pid)
            if action is None:
                action = QAction(self.connection_menu)
                action.setCheckable(True)
                action.setData(pid)
                action.triggered.connect(lambda checked=False, pid=pid: self.select_connection(pid))
                self.connection_group.addAction(action)
                self.connection_actions[pid] = action
            title = profile.get("label", pid)
            model = profile.get("model") or ("Codex 默认模型" if profile.get("kind") == "codex" else "未选择模型")
            action.setText(menu_text(f"{title} · {model}"))
            action.setChecked(pid == provider)
            self.connection_menu.removeAction(action)
            self.connection_menu.insertAction(self.connection_separator, action)
        if p.get("kind") == "codex":
            status = "本机 ChatGPT 登录额度"
        else:
            ready = bool(p.get("key_encrypted")) or bool(os.environ.get(p.get("key_env", "")))
            status = "Key 已配置" if ready else "Key 尚未填写"
        model = p.get("model") or ("Codex 默认模型" if p.get("kind") == "codex" else "未选择模型")
        self.connection_status_action.setText(menu_text(f"当前：{p.get('label', provider)} · {model} · {status}"))
        title = self.current_target_title if self.current_target_host == self.config["ssh"]["host"] else "当前 Chat / Code 会话"
        self.target_action.setText(menu_text(f"当前目标：{self.config['ssh']['host']} · {title}"))
        self.update_menu_state()

    def select_connection(self, connection):
        if self.busy:
            return
        if isinstance(connection, int) and not isinstance(connection, bool):
            ids = list(self.config["profiles"])
            pid = ids[connection] if 0 <= connection < len(ids) else None
        else:
            pid = connection if isinstance(connection, str) else None
        if pid in self.config["profiles"] and pid != self.config["provider"]:
            self.config["provider"] = pid
            save_config(self.config)
            self.update_summary()
            self.reply_failures.clear()
            self.render_reply()
            self.maybe_translate_reply()
            self.statusBar().showMessage("已切换翻译连接：" + self.config["profiles"][pid].get("label", pid))

    def source_changed(self):
        if self.english.toPlainText() and self.draft_source != self.source.toPlainText():
            self.note.setText("中文已修改。请重新翻译，或编辑英文稿后再发送。")

    def english_changed(self):
        if not self.busy:
            self.draft_source = self.source.toPlainText()
            self.note.setText("英文稿已编辑，将发送这里显示的内容。")

    def set_busy(self, value, message="准备就绪"):
        self.busy = value
        self.badge.setText(message)
        for widget in self.controls:
            widget.setEnabled(not value)
        self.update_menu_state()
        self.source.setReadOnly(value)
        self.english.setReadOnly(value)
        if not value:
            self.cancel_upload_button.setEnabled(False)
            self.cancel_upload_button.setVisible(False)
            self.transfer_stage = "idle"

    def update_menu_state(self):
        actions = [*self.settings_actions.values(), *self.connection_actions.values(),
                   self.manage_connections_action, *self.ubuntu_actions.values(), *self.history_actions]
        if self.history_action is not None:
            actions.append(self.history_action)
        for action in actions:
            action.setEnabled(not self.busy)
        self.connection_group.setEnabled(not self.busy)
        if self.history_list is not None:
            self.history_list.setEnabled(not self.busy)
        if self.project_dialog is not None:
            self.project_dialog.update_controls()

    def job(self, message, work, done, failed=None, quiet=False):
        if self.busy:
            return
        self.capture_epoch += 1
        self.set_busy(True, message)
        job = Job(work)
        self.jobs.append(job)
        def success(result):
            self.set_busy(False)
            self.jobs.remove(job)
            self.statusBar().clearMessage()
            done(result)
            self.flush_pending_capture()
        def failure(error):
            self.set_busy(False, "操作未完成")
            self.jobs.remove(job)
            if failed:
                failed(error)
            self.statusBar().showMessage(error)
            if not quiet:
                QMessageBox.warning(self, "操作未完成", error)
            self.flush_pending_capture()
        job.signals.done.connect(success)
        job.signals.failed.connect(failure)
        QThreadPool.globalInstance().start(job)

    def settings(self, section="api"):
        if self.busy:
            return
        dialog = Settings(self, section if isinstance(section, str) else "api")
        available = self.screen().availableGeometry()
        dialog.resize(min(760, available.width() - 60), min(815, available.height() - 65))
        if dialog.exec():
            self.config = dialog.config
            self.connection_configured = True
            self.update_summary()
            self.reply_failures.clear()
            self.render_reply()
            self.maybe_translate_reply()
        dialog.deleteLater()

    def pick_project(self):
        if self.busy or self.closing:
            return
        if self.project_dialog is not None:
            self.project_dialog.raise_()
            self.project_dialog.activateWindow()
            return
        dialog = ProjectPicker(self)
        self.project_dialog = dialog

        def finished(_result):
            if self.project_dialog is dialog:
                self.project_dialog = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()

    def project_switched(self, result):
        context = (result.get("target") or {}).get("context")
        if not isinstance(context, str) or not context:
            self.statusBar().showMessage("尚未确认目标会话，未读取回复。请重新选择项目。")
            return
        projects = [project for project in result.get("projects", []) if project["id"] == result.get("active_project_id")]
        project = projects[0] if len(projects) == 1 else None
        sessions = [session for session in project["sessions"] if session["id"] == result.get("active_session_id")] if project else []
        if result.get("matched"):
            title = " · ".join(value for value in (result.get("project_title"), result.get("session_title"))
                               if isinstance(value, str) and value.strip()) or (result.get("target") or {}).get("title") or "当前 Code 会话"
        else:
            title = " · ".join([project["title"], sessions[0]["title"]]) if project and len(sessions) == 1 else "当前 Code 会话"
        self.current_target_title = title
        self.current_target_host = self.config["ssh"]["host"]
        self.current_target_context = context
        self.target_action.setText(menu_text(f"当前目标：{self.config['ssh']['host']} · {title}"))
        self.capture_epoch += 1
        self.pending_reply_context = context
        self.reply_read_status = "正在读取最后一条回复…"
        self.reply_read_error = ""
        if result.get("switched") or self.reply_context != context:
            self.last_fingerprint = None
            self.reply_original = ""
            self.reply_code_blocks = []
            self.reply_generating = False
            self.reply_failures.clear()
            self.render_reply()
        self.note.setText(("已匹配当前对话：" if result.get("matched") else "已切换到 " if result.get("switched") else "当前会话：") + title + "。")
        self.statusBar().showMessage(result.get("target", {}).get("reason") or "Ubuntu Code 会话已切换。")
        self.refresh(quiet=True)

    def match_current(self):
        if self.busy or self.closing:
            return
        self.capture_epoch += 1
        self.current_target_title = ""
        self.current_target_context = ""
        self.update_summary()
        target = dict(self.config["ssh"])
        self.pending_reply_context = ""
        self.reply_original = ""
        self.reply_code_blocks = []
        self.reply_generating = False
        self.reply_failures.clear()
        self.reply_read_status = "正在匹配当前对话…"
        self.reply_read_error = ""
        self.render_reply()
        self.terminal.setPlaceholderText("正在匹配 Ubuntu 当前对话并读取最新回复…")

        def done(result):
            if target != self.config["ssh"]:
                self.statusBar().showMessage("Ubuntu 连接设置已变化，请重新匹配当前对话。")
                return
            context = (result.get("target") or {}).get("context")
            if not result.get("matched") or not context or context != result.get("context"):
                self.statusBar().showMessage("尚未确认 Ubuntu 当前对话，请重新匹配。")
                return
            self.project_switched(result)

        def failed(message):
            self.pending_reply_context = ""
            self.reply_read_status = "匹配未完成，请重试"
            self.reply_read_error = message
            self.terminal.setPlaceholderText("未能匹配当前对话。请在 Ubuntu 打开目标 Code 会话后重试。")
            self.render_reply()

        self.job("正在匹配当前对话…", lambda: SSH(target).action("match_current", timeout=90), done, failed)

    def enter_changed(self, checked):
        self.config["auto_enter"] = checked
        save_config(self.config)

    def translator(self):
        p = self.config["profiles"][self.config["provider"]]
        if p.get("kind") == "codex":
            return CodexTranslator(p)
        key = unprotect_key(p.get("key_encrypted", "")) or os.environ.get(p.get("key_env", ""), "")
        return Translator(p, key)

    def translate(self, send_after=False):
        if self.busy:
            return
        source = self.source.toPlainText()
        if not source.strip() and self.attachments:
            source = "请查看这些附件，并说明你能从中读取的信息。"
            self.source.setPlainText(source)
        try:
            validate_text(source)
            client = self.translator()
        except BridgeError as exc:
            QMessageBox.warning(self, "无法翻译", str(exc))
            return
        def done(result):
            self.english.setPlainText(result["text"])
            self.draft_source = source
            tokens = result["usage"].get("total_tokens")
            self.note.setText(f"已翻译 · {result['model']}" + (f" · {tokens} tokens" if tokens else "") + "。英文稿可继续编辑。")
            if send_after:
                self.send()
        self.job("正在翻译…", lambda: client.translate(source), done)

    def send(self):
        if self.busy:
            return
        source, english = self.source.toPlainText(), self.english.toPlainText()
        paths = list(self.attachments)
        if not english.strip() and paths:
            english = "Please inspect the attached files and describe what you can determine."
            self.english.setPlainText(english)
        target = dict(self.config["ssh"])
        try:
            validate_text(english)
            if source != self.draft_source:
                raise BridgeError("中文指令已变化，请重新翻译或编辑英文稿后再发送。")
            if any(self.delivery_draft_matches(record) and record["target"] == target
                   for record in self.pending_deliveries.values()):
                raise BridgeError("这条指令的结果仍在核对中，不会重复提交。请先查看 Ubuntu 或等待原编号回执。")
            client = SSH(target)
            rid = uuid.uuid4().hex
            history_record(source, english, target, rid, "sending")
        except (BridgeError, OSError) as exc:
            QMessageBox.warning(self, "未发送", str(exc))
            return
        enter = self.enter.isChecked()
        previews = self.media_previews.isChecked()
        previous_fingerprint = self.last_fingerprint
        self.cancel_transfer.clear()
        self.transfer_stage = "upload" if paths else "idle"
        receipts = []
        delivery_started = False
        delivery = {}
        draft_fingerprint = None
        def work():
            nonlocal delivery_started, draft_fingerprint
            bundle = None
            ready = client.action("check_ready", timeout=90)
            if (paths and ready.get("mode") == "Code"
                    and (ready.get("capabilities") or {}).get("native_attachments") is not True):
                raise BridgeError("当前 Code 附件载入尚未就绪，请刷新后重试。")
            if paths:
                bundle = build_bundle(paths, previews, self.cancel_transfer, self.transfer_progress.emit)
                receipts.extend({"name": f["name"], "local_path": f["path"], "kind": f["kind"], "size": f["size"], "sha256": f["sha256"]} for f in bundle["originals"])
            fingerprint = send_fingerprint(english, target, receipts)
            draft_fingerprint = fingerprint
            if fingerprint == previous_fingerprint:
                raise BridgeError("这份英文稿及附件已送达。请先查看 Ubuntu 客户端；修改内容后可以再次发送。")
            if bundle:
                history_record(source, english, target, rid, "uploading", attachments=receipts)
                uploaded = upload_bundle(client, bundle, self.cancel_transfer, self.transfer_progress.emit)
                receipts[:] = uploaded
            with self.transfer_lock:
                check_cancel(self.cancel_transfer)
                self.transfer_stage = "submitting"
            if bundle:
                self.transfer_progress.emit("附件校验完成，正在发送英文指令…")
            delivered_text = validate_text(compose_prompt(english, receipts, desktop=True))
            identity = delivery_fingerprint(delivered_text, bundle["batch_id"] if bundle else None, ready.get("context"), enter)
            delivery.update(fingerprint=identity, context=ready.get("context"), auto_enter=enter, local_paths=paths)
            history_record(source, english, target, rid, "sending", attachments=receipts, delivery=delivery)
            delivery_started = True
            try:
                result = client.action("send", text=delivered_text, request_id=rid, enter=enter, context=ready.get("context"),
                                       timeout=240 if bundle or ready.get("new_session") else 180,
                                       attachment_batch=bundle["batch_id"] if bundle else None)
            except Exception as exc:
                recovered = self.query_delivery(client, rid, delivery)
                if recovered:
                    return {"state": recovered, "fingerprint": fingerprint, "recovered": True}
                if getattr(exc, "not_submitted", False):
                    delivery_started = False
                    raise
                return {"state": "unknown", "fingerprint": fingerprint, "error": str(exc)}
            if result.get("state") not in {"submitted", "pasted"}:
                recovered = self.query_delivery(client, rid, delivery)
                result = {**result, "state": recovered or "unknown"}
            return {**result, "fingerprint": fingerprint}
        def done(result):
            self.last_fingerprint = result["fingerprint"]
            state = result.get("state", "unknown")
            record = {"id": rid, "source": source, "english": english, "target": target,
                      "attachments": list(receipts), "delivery": dict(delivery)}
            self.finish_delivery(record, state, result.get("error", ""))
        def failed(error):
            record = {"id": rid, "source": source, "english": english, "target": target,
                      "attachments": list(receipts), "delivery": dict(delivery)}
            if delivery_started and delivery:
                self.last_fingerprint = draft_fingerprint
                self.finish_delivery(record, "unknown", error)
                return
            written = self.write_delivery_history(record, "not_sent", error)
            if not delivery_started:
                self.badge.setText("尚未发送")
            self.upload_status.setText(error + ("；本机发送记录写入失败。" if not written else ""))
            self.load_history()
        self.job("正在准备附件…" if paths else "正在发送…", work, done, failed)
        self.cancel_upload_button.setEnabled(bool(paths))
        self.cancel_upload_button.setVisible(bool(paths))

    @staticmethod
    def query_delivery(client, request_id, delivery):
        try:
            receipt = client.action("delivery_status", request_id=request_id,
                                    fingerprint=delivery["fingerprint"], timeout=25)
        except Exception:
            return None
        if not isinstance(receipt, dict) or receipt.get("request_id") != request_id:
            return None
        return confirmed_delivery(receipt, delivery["fingerprint"], delivery["auto_enter"])

    def delivery_draft_matches(self, record):
        return (self.source.toPlainText() == record["source"]
                and self.english.toPlainText() == record["english"]
                and self.attachments == record.get("delivery", {}).get("local_paths", []))

    @staticmethod
    def write_delivery_history(record, state, detail=""):
        try:
            history_record(record["source"], record["english"], record["target"], record["id"],
                           state, detail, attachments=record.get("attachments"), delivery=record.get("delivery") or None)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def finish_delivery(self, record, state, error="", *, background=False):
        rid = record["id"]
        confirmed = state in {"submitted", "pasted"}
        if confirmed:
            self.pending_deliveries.pop(rid, None)
            self.resolved_deliveries.add(rid)
        else:
            self.pending_deliveries[rid] = record
        written = self.write_delivery_history(record, state if confirmed else "unconfirmed", error)
        unchanged = self.delivery_draft_matches(record)
        if not self.busy:
            if confirmed and unchanged:
                self.last_fingerprint = send_fingerprint(record["english"], record["target"], record.get("attachments"))
            if state == "submitted" and unchanged:
                self.clear_submitted_draft()
            self.badge.setText({"submitted": "已发送到客户端", "pasted": "已载入客户端草稿"}.get(state, "结果未确认"))
            if state == "submitted":
                self.note.setText("已核对原编号，消息已提交。" + ("输入内容已清空。" if unchanged else "当前编辑的草稿已保留。"))
            elif state == "pasted":
                self.note.setText("已载入 Ubuntu 客户端草稿，尚未提交。可在 Ubuntu 检查后发送。")
            else:
                self.note.setText("发送结果尚未确认，英文稿已保留；正在只读核对原编号，不会重复提交。")
            warnings = [r["name"] for r in record.get("attachments", []) if r.get("warning")]
            detail = error if not confirmed else "原件已送达；部分预览不可用：" + "、".join(warnings) if warnings else ""
            self.upload_status.setText(detail + ("；本机发送记录写入失败，提交状态仍然有效。" if not written and confirmed else
                                                "；本机发送记录写入失败。" if not written else ""))
        self.load_history()
        if confirmed and not self.busy and not self.closing:
            self.refresh(quiet=True)

    def poll_deliveries(self):
        if not self.connection_configured or self.closing or self.delivery_job is not None or not self.pending_deliveries:
            return
        pending = list(self.pending_deliveries.values())
        record = pending[self.delivery_cursor % len(pending)]
        self.delivery_cursor += 1
        job = Job(lambda: self.query_delivery(SSH(record["target"]), record["id"], record["delivery"]))
        self.delivery_job = job
        def done(state):
            self.delivery_job = None
            if not self.closing and state and record["id"] in self.pending_deliveries:
                self.finish_delivery(record, state, background=True)
        def failed(_error):
            self.delivery_job = None
        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(job)

    def setup_remote(self):
        client = SSH(self.config["ssh"])
        self.job("正在配置 Ubuntu…", client.install,
                 lambda r: self.statusBar().showMessage("Ubuntu 桌面连接已配置。请点击“打开 / 显示客户端”。"))

    def probe(self):
        def done(result):
            mode = "Claude Code" if result.get("mode") == "Code" else "Claude Chat"
            self.statusBar().showMessage(f"SSH 已连接 · {mode} · {result.get('reason', '客户端已连接')}")
        self.job("正在测试 SSH…", lambda: SSH(self.config["ssh"]).action("probe"), done)

    def start(self):
        def done(result):
            self.pending_reply_context = ""
            self.statusBar().showMessage("Ubuntu Claude 桌面客户端已打开，沿用当前对话。")
            self.auto_refresh.setChecked(True)
            self.refresh(quiet=True)
        self.job("正在打开客户端…", lambda: SSH(self.config["ssh"]).action("start"), done)

    def new_session(self):
        def done(result):
            self.pending_reply_context = ""
            self.last_fingerprint = None
            self.statusBar().showMessage(
                "已打开 Code 新会话。请在 Ubuntu 选择 This computer 和项目目录，然后在这里发送首条指令。"
                if result.get("mode") == "Code" else "已在 Ubuntu 客户端打开新对话。")
            self.refresh(quiet=True)
        self.job("正在新建对话…", lambda: SSH(self.config["ssh"]).action("new"), done)

    def refresh(self, checked=False, quiet=False):
        if not self.connection_configured:
            if not quiet:
                self.statusBar().showMessage("请先在设置中保存自己的 Ubuntu SSH 连接。")
            return
        if self.closing:
            return
        if self.busy:
            self.capture_pending = True
            return
        if self.capture_job:
            if not quiet:
                self.capture_pending = True
            return
        self.capture_pending = False
        epoch = self.capture_epoch
        expected_context = self.pending_reply_context
        target = dict(self.config["ssh"])
        try:
            client = SSH(target)
        except BridgeError as exc:
            self.reply_read_failed(str(exc), expected_context)
            self.statusBar().showMessage(str(exc))
            return
        self.reply_read_status = "正在读取最后一条回复…"
        self.reply_read_error = ""
        if not self.reply_original:
            self.terminal.setPlaceholderText(self.reply_read_status)
        self.render_reply()
        job = Job(lambda: client.action("capture", context=expected_context, timeout=REPLY_READ_TIMEOUT) if expected_context
                  else client.action("capture", timeout=REPLY_READ_TIMEOUT))
        self.capture_job = job
        def done(result):
            self.capture_job = None
            if self.closing:
                return
            if epoch != self.capture_epoch or target != self.config["ssh"] or self.busy:
                self.capture_pending = True
            elif expected_context and (expected_context != self.pending_reply_context or result.get("context") != expected_context):
                message = "回复不属于刚选中的会话，已忽略。下次刷新将读取 Ubuntu 当前对话。"
                self.reply_read_failed(message, expected_context)
                self.statusBar().showMessage(message)
            else:
                if expected_context:
                    self.pending_reply_context = ""
                self.reply_context = result.get("context", "")
                if self.current_target_host != target["host"] or self.current_target_context != self.reply_context:
                    title = result.get("title")
                    self.current_target_title = title.strip() if isinstance(title, str) and title.strip() else "当前 Chat / Code 会话"
                    self.current_target_host = target["host"]
                    self.current_target_context = self.reply_context
                    self.target_action.setText(menu_text(f"当前目标：{target['host']} · {self.current_target_title}"))
                self.accept_reply(result)
            self.flush_pending_capture()
        def failed(error):
            self.capture_job = None
            if self.closing:
                return
            if epoch != self.capture_epoch or target != self.config["ssh"] or self.busy:
                self.capture_pending = True
                self.flush_pending_capture()
                return
            self.reply_read_failed(error, expected_context)
            self.statusBar().showMessage(error)
            if not quiet:
                QMessageBox.warning(self, "读取回复未完成", error)
            self.flush_pending_capture()
        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(job)

    def reply_read_failed(self, message, expected_context=""):
        if expected_context and self.pending_reply_context == expected_context:
            # This is only a one-read view binding. A later read may safely follow
            # the native current page; it never submits or changes a task.
            self.pending_reply_context = ""
        self.reply_read_status = "读取未完成，保留上次回复" if self.reply_original else "读取未完成，请重试"
        self.reply_read_error = message
        self.terminal.setPlaceholderText("最后一条回复读取未完成：" + message)
        self.render_reply()

    def flush_pending_capture(self):
        if self.capture_pending and not self.busy and not self.capture_job and not self.closing:
            QTimer.singleShot(0, lambda: self.refresh(quiet=True))

    def current_reply_key(self):
        pid = self.config["provider"]
        return translation_key(self.reply_original, self.config["profiles"][pid], pid, self.reply_code_blocks)

    def set_reply_text(self, value):
        if self.terminal.toPlainText() == value:
            return
        bar = self.terminal.verticalScrollBar()
        at_end, old = bar.value() >= bar.maximum() - 8, bar.value()
        self.terminal.setPlainText(value)
        bar.setValue(bar.maximum() if at_end else old)

    def render_reply(self):
        value = self.reply_original
        status = ""
        if self.reply_translate.isChecked() and value:
            key = self.current_reply_key()
            cached = self.reply_cache.get(key, value)
            if cached:
                value = cached["text"]
            elif self.reply_generating:
                status = "回复完成后自动翻译"
            elif key in self.reply_failures:
                status = "翻译失败，显示原文"
            else:
                status = "正在翻译…" if self.reply_job else "等待翻译…"
            self.reply_translation_status.setToolTip(self.reply_failures.get(key, ""))
        self.reply_translation_status.setText(" · ".join(value for value in (self.reply_read_status, status) if value))
        if self.reply_read_error:
            self.reply_translation_status.setToolTip(self.reply_read_error)
        elif not status:
            self.reply_translation_status.setToolTip("")
        self.set_reply_text(value)

    def accept_reply(self, result):
        self.reply_heading.setText("Claude Code 回复" if result.get("mode") == "Code" else "Claude 回复")
        self.reply_original = result.get("text", "").rstrip("\r\n")
        self.reply_code_blocks = [x for x in result.get("code_blocks", []) if isinstance(x, str)]
        self.reply_generating = bool(result.get("generating", False))
        self.reply_read_error = ""
        self.reply_read_status = "" if self.reply_original else result.get("read_reason") or (
            "Claude 正在生成最后一条回复，暂未返回正文…" if self.reply_generating else "当前会话暂未读到 Claude 回复。")
        self.terminal.setPlaceholderText(self.reply_read_status or "只显示当前会话最后一条 Claude 回复。拖动上方分隔条调整阅读区高度。")
        self.render_reply()
        self.maybe_translate_reply()

    def reply_translation_toggled(self, checked):
        self.config["auto_translate_reply"] = checked
        save_config(self.config)
        if checked:
            self.reply_failures.pop(self.current_reply_key(), None)
            self.auto_refresh.setChecked(True)
            if not self.reply_original and not self.busy:
                self.refresh(quiet=True)
        self.render_reply()
        self.maybe_translate_reply()

    def maybe_translate_reply(self):
        if not self.reply_translate.isChecked() or not self.reply_original or self.reply_generating or self.reply_job:
            return
        key = self.current_reply_key()
        source, blocks = self.reply_original, list(self.reply_code_blocks)
        if self.reply_cache.get(key, source) or key in self.reply_failures:
            return
        try:
            translator = self.translator()
        except BridgeError as exc:
            self.reply_failures[key] = str(exc)
            self.render_reply()
            return
        job = Job(lambda: translator.translate_reply(source, code_blocks=blocks))
        self.reply_job = job
        self.render_reply()
        def done(result):
            self.reply_job = None
            try:
                self.reply_cache.put(key, source, result)
            except OSError:
                self.statusBar().showMessage("译文已完成，但本机缓存未能保存。")
            self.render_reply()
            self.maybe_translate_reply()
        def failed(error):
            self.reply_job = None
            self.reply_failures[key] = error
            self.render_reply()
            self.maybe_translate_reply()
        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(job)

    def load_history(self):
        self.history = []
        directory = DATA_DIR / "history"
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100] if directory.exists() else []
        for path in files:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                self.history_caption(record)
                self.history.append(record)
                delivery = record.get("delivery")
                if (record["id"] not in self.resolved_deliveries and record["state"] in {"sending", "uploading", "unconfirmed", "unknown"}
                        and isinstance(delivery, dict) and isinstance(delivery.get("fingerprint"), str)
                        and len(delivery["fingerprint"]) == 64
                        and all(c in "0123456789abcdef" for c in delivery["fingerprint"])
                        and isinstance(delivery.get("auto_enter"), bool)
                        and isinstance(delivery.get("local_paths"), list)
                        and all(isinstance(value, str) for value in delivery["local_paths"])
                        and isinstance(record.get("target"), dict)):
                    self.pending_deliveries.setdefault(record["id"], record)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
        for action in self.history_actions:
            self.history_menu.removeAction(action)
            action.deleteLater()
        if self.history_empty_action is not None:
            self.history_menu.removeAction(self.history_empty_action)
            self.history_empty_action.deleteLater()
            self.history_empty_action = None
        self.history_actions = []
        for record in self.history[:8]:
            action = self.history_menu.addAction(menu_text(self.history_caption(record).replace("\n", " · ")))
            action.setData(record)
            action.triggered.connect(lambda checked=False, record=record: self.restore_history(record))
            self.history_actions.append(action)
        if not self.history:
            self.history_empty_action = self.history_menu.addAction("暂无发送记录")
            self.history_empty_action.setEnabled(False)
        self.populate_history_list()
        self.update_menu_state()

    def history_caption(self, record):
        names = {"submitted": "已提交", "pasted": "已载入草稿", "sending": "待确认", "uploading": "上传中", "unconfirmed": "未确认", "not_sent": "尚未发送"}
        when = datetime.fromisoformat(record["time"]).astimezone().strftime("%m-%d %H:%M")
        snippet = (record["source"] or record["english"]).replace("\n", " ")[:24]
        if record.get("attachments"):
            snippet += f"  [附件 {len(record['attachments'])}]"
        return f"{when}  {names.get(record['state'], record['state'])}\n{snippet}"

    def populate_history_list(self):
        if self.history_list is None:
            return
        self.history_list.clear()
        for record in self.history:
            item = QListWidgetItem(self.history_caption(record))
            item.setData(Qt.ItemDataRole.UserRole, record)
            self.history_list.addItem(item)
        self.history_list.setCurrentRow(-1)
        self.history_list.clearSelection()

    def show_history_dialog(self):
        if self.busy:
            return
        if self.history_dialog is not None:
            self.history_dialog.raise_()
            self.history_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Claude Bridge · 发送记录")
        dialog.resize(720, 620)
        self.history_dialog = dialog
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("单击一条记录，即可载入原文、英文稿和附件，并关闭此窗口。", "muted", True))
        self.history_list = QListWidget(dialog)
        self.history_list.setAccessibleName("发送记录列表")
        self.history_list.itemClicked.connect(self.open_history)
        layout.addWidget(self.history_list, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        self.load_history()
        try:
            dialog.exec()
        finally:
            self.history_list = None
            self.history_dialog = None
            dialog.deleteLater()

    def open_history(self, item):
        if self.busy or item is None:
            return
        if self.restore_history(item.data(Qt.ItemDataRole.UserRole)) and self.history_dialog is not None:
            self.history_dialog.accept()

    def restore_history(self, r):
        if self.busy:
            return False
        self.source.setPlainText(r["source"])
        self.english.setPlainText(r["english"])
        self.draft_source = r["source"]
        self.attachments = [a["local_path"] for a in r.get("attachments", []) if a.get("local_path")]
        self.refresh_attachment_list()
        self.last_fingerprint = send_fingerprint(r["english"], r["target"], r.get("attachments")) if r["state"] in ("submitted", "pasted") else None
        self.note.setText(f"已载入记录 · {r['state']}。发送目标是 Ubuntu 客户端当前打开的对话。")
        self.source.setFocus()
        return True

    def closeEvent(self, event):
        if self.busy or self.reply_job:
            self.statusBar().showMessage("操作仍在进行，请等完成后关闭；Ubuntu 会话会继续保留。")
            event.ignore()
        else:
            if self.project_dialog is not None:
                self.project_dialog.reject()
            self.closing = True
            self.capture_pending = False
            self.timer.stop()
            self.delivery_timer.stop()
            self.layout_timer.stop()
            self.save_layout()
            self.save_draft()
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setApplicationName("Claude Bridge")
    try:
        window = Window()
    except BridgeError as exc:
        QMessageBox.critical(None, "无法启动", str(exc))
        return 1
    available = app.primaryScreen().availableGeometry()
    window.resize(min(1420, available.width() - 60), min(940, available.height() - 50))
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
