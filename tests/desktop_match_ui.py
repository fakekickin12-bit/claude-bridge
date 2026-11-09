"""One-click current-task matching with fake SSH and controlled worker order."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-match-ui-") as directory:
        data = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication
        import app as desktop
        import core

        config = core.defaults()
        config["auto_translate_reply"] = False
        config["ssh"]["host"] = "synthetic-match-host"
        core.atomic_json(data / "config.json", config)
        qt = QApplication.instance() or QApplication([])
        calls = []
        current = "b" * 64
        state = {"wrong_capture": False, "failure": False,
                 "external_context": "a" * 64, "external_title": "Older task"}

        class FakeSSH:
            def __init__(self, target):
                assert target["host"] == "synthetic-match-host"

            def action(self, action, **kwargs):
                calls.append((action, dict(kwargs)))
                if action == "match_current":
                    assert kwargs == {"timeout": 90}, "Matching must not send drafts or require the old context"
                    if state["failure"]:
                        raise core.BridgeError("Synthetic current task is ambiguous")
                    return {"matched": True, "context": current, "project_title": "sample-project",
                            "session_title": "Current running task", "target": {"context": current,
                                "mode": "Code", "title": "Current running task", "ready": False,
                                "busy": True, "approval": True, "draft": "Native draft stays untouched",
                                "reason": "正在读取当前会话"}}
                assert action == "capture", "Matching must not navigate, check readiness, submit, or start a task"
                if kwargs.get("context"):
                    assert kwargs == {"context": current, "timeout": 90}
                    return {"context": "c" * 64 if state["wrong_capture"] else current,
                            "mode": "Code", "text": "Latest reply of the current running task", "generating": True}
                return {"context": state["external_context"], "mode": "Code", "title": state["external_title"],
                        "text": "Latest external reply"}

        class Pool:
            def __init__(self): self.pending = []
            def start(self, job): self.pending.append(job)
            def one(self, index=0):
                self.pending.pop(index).run()
                qt.processEvents()
            def drain(self):
                for _ in range(30):
                    qt.processEvents()
                    if not self.pending: return
                    self.one()
                raise AssertionError("Match UI worker queue did not settle")

        pool = Pool()
        with patch("app.SSH", FakeSSH), patch("app.QThreadPool") as threads, \
             patch("app.Translator", side_effect=AssertionError("No model calls")), \
             patch("app.CodexTranslator", side_effect=AssertionError("No model calls")), \
             patch("app.QMessageBox.warning") as warning:
            threads.globalInstance.return_value.start.side_effect = pool.start
            window = desktop.Window()
            for timer in (window.timer, window.delivery_timer, window.layout_timer, window.draft_timer): timer.stop()
            window.show()
            window.source.setPlainText("保留中文草稿")
            window.english.setPlainText("Keep the English draft")
            attachment = data / "keep.txt"
            attachment.write_text("keep", encoding="utf-8")
            window.add_attachments([str(attachment)])
            original = (window.source.toPlainText(), window.english.toPlainText(), list(window.attachments),
                        window.config["provider"], list(window.history))
            window.reply_original = "Cached old task reply"
            window.reply_context = "a" * 64
            window.render_reply()

            # The current task may be running, asking for approval, or contain a draft.
            assert window.ubuntu_actions["match"].text() == "匹配当前对话"
            assert window.ubuntu_actions["match"].isEnabled()
            window.refresh(quiet=True)
            assert window.capture_job and len(pool.pending) == 1
            window.ubuntu_actions["match"].trigger()
            assert window.busy and not window.reply_original and not window.terminal.toPlainText()
            assert "正在匹配" in window.terminal.placeholderText()
            assert not window.ubuntu_actions["match"].isEnabled()
            # Complete matching before the stale pre-match read returns.
            pool.one(1)
            assert not window.busy and window.pending_reply_context == current
            assert "sample-project" in window.target_action.text() and "Current running task" in window.target_action.text()
            pool.one()
            assert not window.reply_original, "The late reply for the old task must stay hidden"
            pool.drain()
            assert window.reply_context == current and not window.pending_reply_context
            assert window.reply_original == "Latest reply of the current running task"
            assert window.reply_generating and not window.busy
            assert "已匹配当前对话" in window.note.text()
            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments,
                    window.config["provider"], window.history) == original
            window.update_summary()
            assert "sample-project" in window.target_action.text(), "Connection summary updates retain the confirmed task title"

            # A native change during the new read cannot show a reply under the matched title.
            state["wrong_capture"] = True
            window.ubuntu_actions["match"].trigger()
            pool.drain()
            assert not window.reply_original and window.pending_reply_context == current
            assert "已忽略" in window.statusBar().currentMessage()
            state["wrong_capture"] = False
            window.refresh(quiet=True)
            pool.drain()
            assert window.reply_original == "Latest reply of the current running task"

            # A later accepted refresh follows an external native switch without keeping the old project label.
            state.update(external_context="d" * 64, external_title="Another native task")
            window.refresh(quiet=True)
            pool.drain()
            assert window.reply_context == "d" * 64 and window.reply_original == "Latest external reply"
            assert window.current_target_context == "d" * 64
            assert "Another native task" in window.target_action.text() and "sample-project" not in window.target_action.text()
            window.update_summary()
            assert "Another native task" in window.target_action.text()

            state["failure"] = True
            epoch = window.capture_epoch
            window.ubuntu_actions["match"].trigger()
            assert window.capture_epoch > epoch
            pool.drain()
            assert not window.busy and not window.reply_original and not window.pending_reply_context
            assert warning.call_count == 1 and "ambiguous" in window.statusBar().currentMessage()
            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments,
                    window.config["provider"], window.history) == original
            # Even a failed match invalidates a read that started before it.
            window.refresh(quiet=True)
            window.ubuntu_actions["match"].trigger()
            pool.one(1)
            assert not window.reply_original
            pool.one()
            assert not window.reply_original
            pool.drain()
            assert window.reply_context == "d" * 64 and window.reply_original == "Latest external reply"
            assert {action for action, _ in calls} == {"match_current", "capture"}
            before = len(calls)
            window.set_busy(True)
            window.match_current()
            window.set_busy(False)
            assert len(calls) == before and not pool.pending
            window.close()
            qt.processEvents()
        print("Desktop match UI: one click, current-task reply, stale-context rejection, busy native read, preserved drafts/files/history, no sends/models OK")


if __name__ == "__main__":
    main()
