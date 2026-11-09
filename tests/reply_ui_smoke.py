"""Reply toggle, caching, and stale-result checks with no model or SSH calls."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["CLAUDE_BRIDGE_DATA"] = os.path.abspath("qa/reply-ui-data")

import threading
import time
import uuid
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from app import Window

application = QApplication([])
window = Window()
window.reply_translate.setChecked(False)
window.auto_refresh.setChecked(False)
window.show()

def settle(predicate):
    end = time.monotonic() + 4
    while time.monotonic() < end:
        application.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("reply UI worker did not settle")

calls = []
gate = threading.Event()
stamp = uuid.uuid4().hex[:8]
code = 'print("preserved code")'
first = "Task complete " + stamp + ".\n\n" + code
second = "Second reply " + stamp
third = "Third reply " + stamp
class FakeTranslator:
    def translate_reply(self, source, code_blocks=None):
        calls.append((source, code_blocks))
        if source == second:
            gate.wait(3)
        return {"text": "中文：" + source, "model": "test-model"}

with patch.object(window, "translator", return_value=FakeTranslator()), patch.object(window, "refresh") as refresh:
    window.accept_reply({"text": first, "code_blocks": [code], "generating": True})
    assert window.terminal.toPlainText() == first
    window.reply_translate.setChecked(True)
    window.auto_refresh.setChecked(False)
    assert not calls  # A partial streaming response must not be repeatedly translated.
    window.accept_reply({"text": first, "code_blocks": [code], "generating": False})
    assert not window.busy
    settle(lambda: window.reply_job is None)
    assert window.terminal.toPlainText() == "中文：" + first
    assert calls == [(first, [code])]
    for _ in range(4):
        window.accept_reply({"text": first, "code_blocks": [code], "generating": False})
    assert len(calls) == 1
    window.reply_translate.setChecked(False)
    assert window.terminal.toPlainText() == first
    window.reply_translate.setChecked(True)
    window.auto_refresh.setChecked(False)
    assert window.terminal.toPlainText() == "中文：" + first and len(calls) == 1
    window.accept_reply({"text": second, "generating": False})
    settle(lambda: len(calls) == 2)
    window.accept_reply({"text": third, "generating": True})
    window.reply_translate.setChecked(False)
    gate.set()
    settle(lambda: window.reply_job is None)
    assert window.terminal.toPlainText() == third  # The older completed translation cannot replace it.
    window.reply_translate.setChecked(True)
    window.auto_refresh.setChecked(False)
    window.accept_reply({"text": third, "generating": False})
    settle(lambda: window.reply_job is None)
    assert window.terminal.toPlainText() == "中文：" + third and len(calls) == 3
    window.reply_translate.setChecked(False)
    assert window.terminal.toPlainText() == third
    assert not refresh.called
window.close()
print("Reply UI: toggle, cache reuse, completed-response-only translation, and stale-result guard OK")
