"""Bounded latest-reply reads, visible failures and read-only context recovery."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-latest-read-ui-") as directory:
        data = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication
        import app as desktop
        import core

        config = core.defaults()
        config["auto_translate_reply"] = False
        config["ssh"]["host"] = "synthetic-latest-host"
        core.atomic_json(data / "config.json", config)
        qt = QApplication.instance() or QApplication([])
        old, current = "a" * 64, "b" * 64
        state = {"failure": True, "empty": False, "wrong": False}
        calls = []

        class FakeSSH:
            def __init__(self, target):
                assert target["host"] == "synthetic-latest-host"

            def action(self, action, **values):
                assert action == "capture", "Reply reads must never send, navigate or invoke models"
                assert values["timeout"] == 60
                calls.append(dict(values))
                if state["failure"]:
                    raise core.BridgeError("Synthetic read timeout")
                return {"context": current if state["wrong"] or not values.get("context") else values["context"],
                        "title": "Current native task", "mode": "Code", "scope": "latest",
                        "text": "" if state["empty"] else "Only the latest reply", "code_blocks": [],
                        "read_status": "empty" if state["empty"] else "ready",
                        "read_reason": "最后一条回复尚未输出内容" if state["empty"] else "",
                        "generating": state["empty"]}

        class Pool:
            def __init__(self): self.pending = []
            def start(self, job): self.pending.append(job)
            def one(self):
                self.pending.pop(0).run()
                qt.processEvents()

        pool = Pool()
        with patch("app.SSH", FakeSSH), patch("app.QThreadPool") as threads, \
             patch("app.Translator", side_effect=AssertionError("No model calls")), \
             patch("app.CodexTranslator", side_effect=AssertionError("No model calls")), \
             patch("app.QMessageBox.warning") as warning:
            threads.globalInstance.return_value.start.side_effect = pool.start
            window = desktop.Window()
            for timer in (window.timer, window.delivery_timer, window.layout_timer, window.draft_timer): timer.stop()
            window.source.setPlainText("保留我的中文草稿")
            window.english.setPlainText("Keep my English draft")
            attachment = data / "keep.txt"
            attachment.write_text("synthetic attachment", encoding="utf-8")
            window.add_attachments([str(attachment)])
            snapshot = (window.source.toPlainText(), window.english.toPlainText(), list(window.attachments))
            window.auto_refresh.setChecked(True)
            window.pending_reply_context = old
            window.refresh(quiet=True)
            assert len(pool.pending) == 1 and not window.busy
            assert "正在读取当前任务回复" in window.reply_translation_status.text()
            assert "正在读取当前任务回复" in window.terminal.placeholderText()
            assert window.settings_actions["api"].isEnabled() and not window.source.isReadOnly()
            for _ in range(3): window.refresh(quiet=True)
            assert len(pool.pending) == 1
            pool.one()
            assert calls[-1]["context"] == old and not window.pending_reply_context
            assert window.auto_refresh.isChecked() and not warning.called
            assert "读取未完成" in window.reply_translation_status.text()
            assert "Synthetic read timeout" in window.terminal.placeholderText()
            assert not pool.pending, "A failed read must wait for the next timer or manual refresh"

            # A fresh read follows the current native context after the failed one-read binding.
            state["failure"] = False
            window.refresh(quiet=True)
            pool.one()
            assert "context" not in calls[-1]
            assert window.reply_context == current and window.reply_original == "Only the latest reply"
            assert "Current native task" in window.target_action.text()
            assert not window.reply_read_status and not window.reply_read_error

            # An empty latest assistant message is visible as a state, not a blank generic box.
            state["empty"] = True
            window.refresh(quiet=True)
            pool.one()
            assert not window.reply_original and window.reply_generating
            assert window.terminal.placeholderText() == "最后一条回复尚未输出内容"
            assert window.reply_translation_status.text() == "最后一条回复尚未输出内容"

            # Mismatched content stays hidden, then the next read may observe the actual page.
            window.pending_reply_context = old
            state.update(empty=False, wrong=True)
            window.refresh(quiet=True)
            pool.one()
            assert not window.reply_original and not window.pending_reply_context
            assert "已忽略" in window.statusBar().currentMessage() and not pool.pending
            window.refresh(quiet=True)
            pool.one()
            assert window.reply_context == current and window.reply_original == "Only the latest reply"

            state["failure"] = True
            window.refresh(quiet=True)
            assert window.terminal.toPlainText() == "Only the latest reply"
            pool.one()
            assert "保留上次回复" in window.reply_translation_status.text()
            assert window.terminal.toPlainText() == "Only the latest reply"
            assert window.auto_refresh.isChecked() and not pool.pending
            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == snapshot
            assert not warning.called
            window.close()
            qt.processEvents()
        print("Latest reply read UI: bounded timeout, visible loading/empty/error, safe current-context recovery, no foreground lock or resubmission OK")


if __name__ == "__main__":
    main()
