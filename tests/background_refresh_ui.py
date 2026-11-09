"""Hold reply reads in flight and verify that they never lock the interface."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["CLAUDE_BRIDGE_DATA"] = os.path.abspath("qa/background-ui-data")

import threading
import time
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from app import Window

application = QApplication([])
window = Window()
window.reply_translate.setChecked(False)
window.auto_refresh.setChecked(False)
window.timer.stop()
window.show()

def settle(predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("background UI worker did not settle")

gate = threading.Event()
reads = []
class ReadClient:
    def __init__(self, config): self.target = dict(config)
    def action(self, action, **kwargs):
        assert action == "capture"
        reads.append(self.target)
        index = len(reads)
        if index == 1:
            gate.wait(3)
        return {"text": "first read" if index == 1 else "fresh read"}

with patch("app.SSH", ReadClient), patch.object(window, "set_busy", wraps=window.set_busy) as busy:
    window.refresh(quiet=True)
    settle(lambda: len(reads) == 1)
    for _ in range(4): window.refresh(quiet=True)
    assert not window.busy and window.settings_actions["api"].isEnabled() and window.send_button.isEnabled()
    assert not window.source.isReadOnly() and not window.english.isReadOnly()
    assert len(reads) == 1 and not busy.called
    window.source.setPlainText("刷新时仍可编辑")
    window.english.setPlainText("Editable during refresh.")
    gate.set()
    settle(lambda: window.capture_job is None)
    assert window.reply_original == "first read" and not busy.called
    assert window.source.toPlainText() == "刷新时仍可编辑"
    assert window.english.toPlainText() == "Editable during refresh."

# A stale read cannot unlock an active send/translation or replace newer context.
reads.clear(); gate.clear()
foreground_gate = threading.Event()
completed = []
def foreground_work():
    foreground_gate.wait(3)
    return True
with patch("app.SSH", ReadClient):
    window.refresh(quiet=True)
    settle(lambda: len(reads) == 1)
    window.job("前台操作", foreground_work, lambda result: completed.append(result))
    assert window.busy and not window.settings_actions["api"].isEnabled()
    gate.set()
    settle(lambda: window.capture_job is None)
    assert window.busy and not window.settings_actions["api"].isEnabled()
    assert window.reply_original == "first read" and window.capture_pending
    foreground_gate.set()
    settle(lambda: completed and not window.busy and window.capture_job is None and not window.capture_pending)
    assert len(reads) == 2 and window.reply_original == "fresh read"
    assert window.settings_actions["api"].isEnabled()

# Changing the SSH target while reading must reject the previous target's result.
reads.clear(); gate.clear()
window.config["ssh"] = {**window.config["ssh"], "host": "initial-test-host"}
with patch("app.SSH", ReadClient), patch.object(window, "accept_reply", wraps=window.accept_reply) as accept:
    window.refresh(quiet=True)
    settle(lambda: len(reads) == 1)
    window.config["ssh"] = {**window.config["ssh"], "host": "other-test-host"}
    gate.set()
    settle(lambda: len(reads) == 2 and window.capture_job is None and not window.capture_pending)
    assert reads[0]["host"] != reads[1]["host"]
    assert accept.call_count == 1 and accept.call_args.args[0]["text"] == "fresh read"

# A failed automatic read stays quiet and keeps editing available.
class FailedClient(ReadClient):
    def action(self, action, **kwargs):
        from core import BridgeError
        raise BridgeError("Synthetic read failure")
with patch("app.SSH", FailedClient), patch("app.QMessageBox.warning") as warning:
    window.badge.setText("保留前台状态")
    window.auto_refresh.setChecked(True)
    window.refresh(quiet=True)
    settle(lambda: window.capture_job is None)
    assert not warning.called and window.auto_refresh.isChecked()
    assert "读取未完成" in window.reply_translation_status.text()
    assert window.settings_actions["api"].isEnabled() and window.badge.text() == "保留前台状态"

# Closing during a read is allowed; its late result cannot update the closed window.
reads.clear(); gate.clear()
with patch("app.SSH", ReadClient), patch.object(window, "accept_reply") as accept:
    window.refresh(quiet=True)
    settle(lambda: len(reads) == 1)
    window.close()
    assert window.closing
    gate.set()
    settle(lambda: window.capture_job is None)
    assert not accept.called
print("Background refresh: controls editable, reads coalesced, stale reads rejected, foreground guard preserved, close safe")
