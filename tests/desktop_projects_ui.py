"""Two-stage project UI checks with synthetic catalogues and controlled workers."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-projects-ui-") as directory:
        data = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import QCoreApplication, QEvent, Qt
        from PySide6.QtWidgets import QApplication
        import app as desktop
        import core

        config = core.defaults()
        config.update(auto_translate_reply=False)
        config["ssh"]["host"] = "synthetic-projects-host"
        core.atomic_json(data / "config.json", config)
        qt = QApplication.instance() or QApplication([])
        calls = []
        state = {"context": "a" * 64, "session": "", "project": "", "read_failure": False,
                 "capture_wrong": False, "open_failure": False}
        alpha, beta, duplicate = "1" * 64, "4" * 64, "6" * 64
        recent, older = "5" * 64, "7" * 64

        class ForbiddenSessions(list):
            def __iter__(self):
                raise AssertionError("Headers/unselected projects must not enumerate session titles")
            def __len__(self):
                raise AssertionError("Headers/unselected projects must not read sessions")

        def catalogue(project=""):
            sessions = [{"id": recent, "title": "Recent task", "active": state["session"] == recent,
                         "selectable": True, "reason": ""},
                        {"id": older, "title": "Older task", "active": state["session"] == older,
                         "selectable": True, "reason": ""},
                        {"id": "8" * 64, "title": "Duplicate task", "active": False,
                         "selectable": False, "reason": "Synthetic duplicate session"}]
            projects = [{"id": alpha, "title": "Alpha", "active": state["project"] == alpha,
                         "selectable": True, "reason": "", "expanded": True,
                         "sessions_loaded": False, "sessions": ForbiddenSessions()},
                        {"id": beta, "title": "Beta", "active": state["project"] == beta,
                         "selectable": True, "reason": "", "expanded": bool(project),
                         "sessions_loaded": project == beta, "sessions": sessions if project == beta else ForbiddenSessions()},
                        {"id": duplicate, "title": "Duplicate project", "active": False,
                         "selectable": False, "reason": "Synthetic duplicate project", "expanded": False,
                         "sessions_loaded": False, "sessions": ForbiddenSessions()}]
            return {"scope": "sessions" if project else "projects", "selected_project_id": project,
                    "session_catalog_id": "f" * 64 if project else "", "context": state["context"],
                    "catalog_id": "b" * 64, "mode": "Code", "projects": projects,
                    "active_project_id": state["project"] or None, "active_session_id": state["session"] or None}

        class FakeSSH:
            def __init__(self, target):
                assert target["host"] == "synthetic-projects-host"

            def action(self, action, **kwargs):
                calls.append((action, dict(kwargs)))
                if action == "list_projects":
                    assert kwargs == {"timeout": 60}
                    if state["read_failure"]:
                        raise core.BridgeError("Synthetic header read failure")
                    return catalogue()
                if action == "capture":
                    if kwargs.get("context"):
                        assert kwargs["context"] == state["context"] and kwargs["timeout"] == 90
                    return {"mode": "Code", "text": "Reply for " + state["session"], "generating": False,
                            "context": "0" * 64 if state["capture_wrong"] else state["context"]}
                assert action in {"open_project", "switch_project"}, "Navigation must not send a message"
                assert kwargs["context"] == state["context"] and kwargs["catalog_id"] == "b" * 64
                assert kwargs["project_id"] == beta and kwargs["timeout"] == 110
                if action == "open_project":
                    assert set(kwargs) == {"context", "catalog_id", "project_id", "timeout"}
                    if state["open_failure"]:
                        raise core.BridgeError("Synthetic project open failure")
                    chosen = state["session"] if state["project"] == beta and state["session"] in {recent, older} else recent
                else:
                    assert kwargs["session_catalog_id"] == "f" * 64
                    assert kwargs["session_id"] == older
                    chosen = older
                switched = state["session"] != chosen
                state.update(context="d" * 64 if chosen == recent else "e" * 64, project=beta, session=chosen)
                return {**catalogue(beta), "switched": switched,
                        "target": {"context": state["context"], "mode": "Code", "ready": False,
                                   "busy": True, "reason": "Synthetic existing task keeps running"}}

        class Pool:
            def __init__(self): self.pending = []
            def start(self, job): self.pending.append(job)
            def one(self):
                assert self.pending
                self.pending.pop(0).run()
                qt.processEvents()
            def drain(self):
                for _ in range(25):
                    qt.processEvents()
                    if not self.pending: return
                    self.one()
                raise AssertionError("Project workers did not settle")

        pool = Pool()
        with patch("app.SSH", FakeSSH), patch("app.QThreadPool") as threads, \
             patch("app.Translator", side_effect=AssertionError("No model calls")), \
             patch("app.CodexTranslator", side_effect=AssertionError("No model calls")), \
             patch("app.QMessageBox.warning") as warning:
            threads.globalInstance.return_value.start.side_effect = pool.start
            window = desktop.Window()
            for timer in (window.timer, window.delivery_timer, window.layout_timer, window.draft_timer): timer.stop()
            window.show()
            window.source.setPlainText("Local Chinese draft")
            window.english.setPlainText("Local English draft")
            attachment = data / "synthetic.txt"
            attachment.write_text("synthetic", encoding="utf-8")
            window.add_attachments([str(attachment)])
            original = (window.source.toPlainText(), window.english.toPlainText(), list(window.attachments))
            window.reply_original = "Old project reply"
            window.render_reply()

            picker = desktop.ProjectPicker(window)
            window.project_dialog = picker
            picker.show()
            qt.processEvents()
            assert picker.loading and not window.busy and not window.source.isReadOnly()
            pool.drain()
            assert calls == [("list_projects", {"timeout": 60})]
            assert picker.project_combo.currentData() == "" and picker.session_list.count() == 0
            duplicate_item = picker.project_combo.model().item(picker.project_combo.findData(duplicate))
            assert not duplicate_item.isEnabled() and "duplicate project" in duplicate_item.toolTip()
            assert window.reply_original == "Old project reply", "Reading names must not change the current reply"

            window.config["ssh"]["host"] = "synthetic-changed-host"
            window.update_summary()
            assert not picker.project_combo.isEnabled()
            picker.project_changed(0)
            assert len(calls) == 1
            window.config["ssh"]["host"] = "synthetic-projects-host"
            window.update_summary()

            picker.project_combo.setCurrentIndex(picker.project_combo.findData(beta))
            assert window.busy and picker.loading and window.source.isReadOnly()
            assert not window.reply_original and "正在读取" in window.terminal.placeholderText()
            assert not window.ubuntu_actions["projects"].isEnabled()
            pool.one()
            assert not window.busy and picker.active_dialog
            assert sum(action == "open_project" for action, _ in calls) == 1
            assert picker.session_list.count() == 3 and picker.session_list.currentItem() is None
            assert picker.session_list.item(0).checkState() == Qt.CheckState.Checked
            assert not picker.session_list.item(2).flags() & Qt.ItemFlag.ItemIsEnabled
            assert "duplicate session" in picker.session_list.item(2).toolTip()
            assert window.pending_reply_context == "d" * 64 and not window.reply_original
            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == original
            window.english.setPlainText("A new English edit while reading")
            pool.drain()
            assert window.reply_original == "Reply for " + recent
            assert not window.pending_reply_context and window.reply_context == "d" * 64
            assert window.english.toPlainText() == "A new English edit while reading"
            assert sum(action == "open_project" for action, _ in calls) == 1, "Rendering a result must not loop navigation"
            window.english.setPlainText(original[1])

            picker.refresh_catalog()
            assert not window.busy and not window.source.isReadOnly()
            pool.drain()
            assert picker.catalog["scope"] == "projects" and picker.project_combo.currentData() == ""
            assert picker.session_list.count() == 0 and sum(action == "open_project" for action, _ in calls) == 1
            picker.project_combo.setCurrentIndex(picker.project_combo.findData(beta))
            pool.drain()
            assert sum(action == "open_project" for action, _ in calls) == 2

            state["capture_wrong"] = True
            picker.session_list.setCurrentRow(1)
            picker.switch_session()
            assert window.busy
            pool.drain()
            assert not picker.active_dialog and not window.reply_original
            assert window.pending_reply_context == "e" * 64
            assert "已忽略" in window.statusBar().currentMessage()
            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == original
            state["capture_wrong"] = False
            window.refresh(quiet=True)
            pool.drain()
            assert window.reply_original == "Reply for " + older and not window.pending_reply_context
            state.update(context="9" * 64, session="external-session")
            window.refresh(quiet=True)
            pool.drain()
            assert window.reply_context == "9" * 64 and window.reply_original == "Reply for external-session"
            window.project_dialog = None
            picker.deleteLater()

            for fail in (False, True):
                state["read_failure"] = fail
                late = desktop.ProjectPicker(window)
                late.show()
                qt.processEvents()
                assert late.loading
                late.reject()
                late.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                pool.drain()
                assert not late.active_dialog and not late.jobs
            state["read_failure"] = False
            late = desktop.ProjectPicker(window)
            late.show()
            pool.drain()
            late.project_combo.setCurrentIndex(late.project_combo.findData(beta))
            assert window.busy
            late.reject()
            late.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            pool.drain()
            assert not window.busy and "Beta" in window.target_action.text()
            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == original

            state["open_failure"] = True
            failed = desktop.ProjectPicker(window)
            failed.show()
            pool.drain()
            failed.project_combo.setCurrentIndex(failed.project_combo.findData(beta))
            pool.drain()
            assert not window.busy and not window.pending_reply_context
            assert "open failure" in failed.status.text()
            assert window.reply_original == "Reply for " + state["session"]
            failed.reject()
            failed.deleteLater()

            # Keep the existing Code attachment gate regression beside navigation.
            delivery_calls = []
            class CodeDelivery:
                capable = "true"
                def __init__(self, target): pass
                def action(self, action, **kwargs):
                    delivery_calls.append((action, kwargs))
                    if action == "check_ready":
                        return {"mode": "Code", "context": state["context"],
                                "capabilities": {"native_attachments": self.capable}}
                    if action == "send":
                        assert self.capable is True and kwargs["enter"] is False
                        return {"state": "pasted"}
                    assert action == "capture"
                    return {"mode": "Code", "text": "Synthetic reply", "context": state["context"]}
            window.enter.setChecked(False)
            with patch("app.SSH", CodeDelivery), patch("app.build_bundle") as build:
                window.send()
                pool.drain()
                assert not build.called and not any(action == "send" for action, _ in delivery_calls)
                assert warning.called
                warning.reset_mock()
                CodeDelivery.capable = True
                entry = {"name": attachment.name, "path": str(attachment), "kind": "file",
                         "size": attachment.stat().st_size, "sha256": "0" * 64}
                build.return_value = {"batch_id": "a" * 32, "originals": [entry]}
                receipt = {**entry, "local_path": str(attachment), "remote_path": "/synthetic/attachment.txt"}
                with patch("app.upload_bundle", return_value=[receipt]) as upload:
                    window.send()
                    pool.drain()
                    assert build.called and upload.called
                    assert any(action == "send" for action, _ in delivery_calls)
                assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == original

            window.set_busy(True)
            with patch("app.ProjectPicker") as factory:
                window.pick_project()
                factory.assert_not_called()
            window.set_busy(False)
            assert not warning.called
            assert all(action in {"list_projects", "open_project", "switch_project", "capture"} for action, _ in calls)
            window.close()
            qt.processEvents()
        print("Desktop projects UI: headers only, one explicit project open, automatic recent reply, session signature, context guards, preserved drafts, and late callback safety OK")


if __name__ == "__main__":
    main()
