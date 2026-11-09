import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["CLAUDE_BRIDGE_DATA"] = os.path.abspath("qa/test-data")

from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QMimeData, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QFontDatabase, QImage
from PySide6.QtWidgets import QApplication
from app import Window, Settings, STYLE

app = QApplication([])
for font in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/consola.ttf"]:
    QFontDatabase.addApplicationFont(font)
app.setStyle("Fusion")
app.setStyleSheet(STYLE)
window = Window()
window.reply_translate.setChecked(False)
window.auto_refresh.setChecked(False)
window.clear_attachments()
window.show()
app.processEvents()
assert window.source.placeholderText()
assert window.config["ssh"]["host"] == "ubuntu"
initial_sizes = window.body_splitter.sizes()
initial_reply_height = window.terminal.height()
window.body_splitter.setSizes([150, 900])
app.processEvents()
assert window.terminal.height() > initial_reply_height
window.body_splitter.setSizes([900, 80])
app.processEvents()
assert window.terminal.height() < initial_reply_height
window.resize(1420, 650)
app.processEvents()
assert window.height() == 650 and window.terminal.height() >= 50
window.resize(1420, 940)
window.body_splitter.setSizes(initial_sizes)
app.processEvents()
window.source.setPlainText("检查项目启动报错，保留现有功能。\n代码块和路径保持原样。")
window.english.setPlainText("Check the project's startup error and preserve the existing functionality.\nKeep code blocks and paths unchanged.")
window.source.insertPlainText("已修改")
assert window.draft_source != window.source.toPlainText()
assert "中文已修改" in window.note.text()
window.draft_source = None
with patch("app.SSH") as ssh, patch("app.QMessageBox.warning") as warning:
    window.send()
    assert warning.called and not ssh.called
window.english.insertPlainText(" Revised.")
assert window.draft_source == window.source.toPlainText()
window.source.clear()
window.english.clear()
window.note.setText("可先检查英文稿，也可一键翻译并发送。代码块与内联代码会原样保留。")
Path("qa").mkdir(exist_ok=True)
source = Path("qa/ui-attachment.txt").resolve()
source.write_text("Synthetic attachment", encoding="utf-8")
mime = QMimeData()
mime.setUrls([QUrl.fromLocalFile(str(source))])
window.english.insertFromMimeData(mime)
window.source.insertFromMimeData(mime)
assert len(window.attachments) == 1 and not window.source.toPlainText()
image = QImage(96, 64, QImage.Format.Format_RGB32)
image.fill(QColor("#2466ae"))
mime = QMimeData()
mime.setImageData(image)
window.source.insertFromMimeData(mime)
assert len(window.attachments) == 2
assert Path(window.attachments[-1]).is_file()
window.attachment_list.setCurrentRow(0)
window.remove_attachment()
assert len(window.attachments) == 1
if Path("qa/media-selftest/sample-video.mp4").exists():
    window.add_attachments([str(Path("qa/media-selftest/sample-video.mp4").resolve()), str(Path("qa/media-selftest/sample-audio.wav").resolve())])
app.processEvents()
window.grab().save("qa/main.png")
dialog = Settings(window)
dialog.provider.setCurrentIndex(dialog.provider.findData("OpenAI"))
dialog.key.setText("fake-local-test-key")
dialog.collect_profile()
before = len(dialog.config["profiles"])
dialog.add_profile(True)
assert len(dialog.config["profiles"]) == before + 1
assert dialog.keys[dialog.active] == "fake-local-test-key"
dialog.profile_name.setText("第二组 OpenAI")
dialog.collect_profile()
assert dialog.config["profiles"][dialog.active]["label"] == "第二组 OpenAI"
dialog.provider.setCurrentIndex(dialog.provider.findData("OpenAI 赠送额度"))
dialog.show()
app.processEvents()
dialog.grab().save("qa/settings.png")
dialog.reject()

# Exercise the media send worker and prompt composition without any network/model call.
packets, failures = [], []
class FakeSSH:
    def __init__(self, config):
        self.config = config
    def action(self, action, **kwargs):
        if action == "check_ready":
            return {"ok": True, "ready": True, "context": "test-context"}
        if action == "send":
            packets.append(kwargs)
            return {"ok": True, "state": "submitted"}
        if action == "capture":
            return {"ok": True, "text": "❯ "}
        raise AssertionError(action)
def fake_upload(client, bundle, *args):
    return [{"name": f["name"], "local_path": f["path"], "kind": f["kind"], "size": f["size"], "sha256": f["sha256"],
             "remote_path": "/workspace/attachments/" + f["id"]} for f in bundle["originals"]]
window.source.setPlainText("查看附件")
window.english.setPlainText("Inspect the attached files.")
window.media_previews.setChecked(False)
with patch("app.SSH", FakeSSH), patch("app.upload_bundle", fake_upload), patch("app.QMessageBox.warning", side_effect=lambda *args: failures.append(args)):
    window.send()
    for _ in range(300):
        app.processEvents()
        if packets and not window.busy and not window.capture_job and not window.capture_pending:
            break
        import time
        time.sleep(.01)
assert not failures, failures
assert len(packets) == 1 and packets[0]["attachment_batch"]
assert "/workspace/attachments/" not in packets[0]["text"]
assert "sampled video frames only" in packets[0]["text"]
assert packets[0]["context"] == "test-context"
assert window.config["ssh"]["transport"] == "desktop"
assert not window.cancel_upload_button.isVisible()
assert not window.source.toPlainText() and not window.english.toPlainText()
assert not window.attachments and window.draft_source is None
import json
from app import DATA_DIR
saved = json.loads((DATA_DIR / "draft.json").read_text(encoding="utf-8"))
assert saved["source"] == saved["english"] == "" and saved["attachments"] == []
record = window.history[0]
assert record["source"] == "查看附件" and record["english"] == "Inspect the attached files."
assert record["state"] == "submitted" and record["attachments"]
assert all(Path(a["local_path"]).is_file() for a in record["attachments"])

# Loading only a remote draft must keep both local inputs and attachment selections.
class DraftSSH(FakeSSH):
    def action(self, action, **kwargs):
        if action == "send":
            assert kwargs["enter"] is False
            packets.append(kwargs)
            return {"ok": True, "state": "pasted"}
        return super().action(action, **kwargs)
window.source.setPlainText("检查任务")
window.english.setPlainText("Inspect the task.")
window.add_attachments([str(source)])
window.enter.setChecked(False)
with patch("app.SSH", DraftSSH), patch("app.upload_bundle", fake_upload), patch("app.QMessageBox.warning"):
    window.send()
    for _ in range(300):
        app.processEvents()
        if len(packets) == 2 and not window.busy and not window.capture_job and not window.capture_pending:
            break
        import time
        time.sleep(.01)
assert window.source.toPlainText() == "检查任务" and window.english.toPlainText() == "Inspect the task."
assert window.attachments == [str(source)]
window.enter.setChecked(True)

# A preflight failure must preserve both drafts and be recorded as definitely not sent.
states = []
class BlockedSSH(FakeSSH):
    def action(self, action, **kwargs):
        from core import BridgeError
        if action == "check_ready":
            raise BridgeError("测试：客户端窗口尚未就绪")
        raise AssertionError("No delivery action is allowed after failed preflight")
before_source, before_english = window.source.toPlainText(), window.english.toPlainText()
with patch("app.SSH", BlockedSSH), patch("app.history_record", side_effect=lambda *args, **kw: states.append(args[4])), \
     patch("app.QMessageBox.warning"):
    window.send()
    for _ in range(200):
        app.processEvents()
        if not window.busy:
            break
        import time
        time.sleep(.01)
assert states[-1] == "not_sent" and window.badge.text() == "尚未发送"
assert window.source.toPlainText() == before_source and window.english.toPlainText() == before_english

class ReadbackFailedSSH(FakeSSH):
    def action(self, action, **kwargs):
        from core import BridgeError
        if action == "send":
            error = BridgeError("测试：文字载入未确认，消息尚未提交")
            error.not_submitted = True
            raise error
        return super().action(action, **kwargs)
window.last_fingerprint = None
states.clear()
with patch("app.SSH", ReadbackFailedSSH), patch("app.upload_bundle", fake_upload), \
     patch("app.history_record", side_effect=lambda *args, **kw: states.append(args[4])), patch("app.QMessageBox.warning"):
    window.send()
    for _ in range(200):
        app.processEvents()
        if not window.busy:
            break
        import time
        time.sleep(.01)
assert states[-1] == "not_sent" and window.badge.text() == "尚未发送"
assert window.english.toPlainText() == before_english
window.source.clear()
window.english.clear()
window.clear_attachments()

result = []
window.job("测试后台任务…", lambda: {"ok": True}, lambda data: result.append(data))
for _ in range(100):
    app.processEvents()
    if result:
        break
    import time
    time.sleep(.01)
assert result == [{"ok": True}] and not window.busy
window.close()
print("UI: source/draft binding, settings, screenshot, background job OK")
