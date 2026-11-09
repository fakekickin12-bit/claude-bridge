"""Top-menu desktop workflows with temporary data and controlled local workers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="bridge-desktop-menus-") as directory:
        data_dir = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data_dir)
        sys.path.insert(0, str(source))

        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtWidgets import QApplication, QDialog, QFrame
        import app as desktop
        import core
        import mobile_devices

        config = core.defaults()
        stable_id = "OpenAI 赠送额度"
        other_id = "synthetic-deepseek-connection"
        profile = core.make_profile("OpenAI 赠送额度")
        profile.update(label="OpenAI-api", model="synthetic-openai-model", key_encrypted="",
                       key_env="SYNTHETIC_MENUS_UNUSED_API_KEY")
        other = core.make_profile("DeepSeek")
        other.update(label="DeepSeek test", model="synthetic-deepseek-model", key_encrypted="",
                     key_env="SYNTHETIC_MENUS_UNUSED_API_KEY")
        config.update(profiles={stable_id: profile, other_id: other}, provider=other_id,
                      auto_translate_reply=False)
        config["ssh"]["host"] = "synthetic-menus-host"
        core.atomic_json(data_dir / "config.json", config)

        attachment = data_dir / "synthetic-attachment.txt"
        attachment.write_text("Synthetic history attachment", encoding="utf-8")
        receipt = {"name": attachment.name, "local_path": str(attachment), "kind": "file",
                   "size": attachment.stat().st_size, "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest()}
        beginning = datetime.now(timezone.utc) - timedelta(minutes=10)
        records = []
        for index in range(10):
            record = {
                "id": f"{index + 1:032x}", "time": (beginning + timedelta(minutes=index)).isoformat(),
                "source": f"历史中文 {index}", "english": f"Synthetic history {index}",
                "target": dict(config["ssh"]), "state": "submitted", "detail": "",
                "attachments": [dict(receipt)]
            }
            path = data_dir / "history" / (record["id"] + ".json")
            core.atomic_json(path, record)
            stamp = beginning.timestamp() + index
            os.utime(path, (stamp, stamp))
            records.append(record)

        application = QApplication.instance() or QApplication([])
        application.setStyle("Fusion")
        application.setStyleSheet(desktop.STYLE)
        calls = []

        class FakeSSH:
            def __init__(self, target):
                assert target["host"] == "synthetic-menus-host"

            def install(self):
                calls.append("setup")
                return {"configured": True}

            def action(self, action, **kwargs):
                calls.append(action)
                if action == "probe":
                    return {"mode": "Code", "title": "Synthetic task", "model": "Synthetic model",
                            "ready": True, "reason": "可以接收指令"}
                if action in {"start", "new"}:
                    return {"mode": "Code", "opened": True}
                if action == "capture":
                    return {"mode": "Code", "text": "Synthetic assistant reply", "generating": False}
                raise AssertionError("This menu test must not send a message or use an unexpected SSH action")

        class ControlledPool:
            def __init__(self):
                self.pending = []

            def start(self, job):
                self.pending.append(job)

            def drain(self):
                for _ in range(20):
                    if not self.pending:
                        application.processEvents()
                        if not self.pending:
                            return
                    if self.pending:
                        self.pending.pop(0).run()
                    application.processEvents()
                raise AssertionError("The controlled desktop worker queue did not settle")

        pool = ControlledPool()
        window = None
        failure = AssertionError("This menu check must not call a real translator")
        with patch("app.SSH", FakeSSH), \
             patch("app.Translator", side_effect=failure) as translator, \
             patch("app.CodexTranslator", side_effect=failure) as codex, \
             patch("app.QThreadPool") as thread_pool, \
             patch("app.QMessageBox.warning") as warning, \
             patch("mobile_devices.issue_pairing_code", side_effect=AssertionError("No pairing code is needed")):
            thread_pool.globalInstance.return_value.start.side_effect = pool.start
            try:
                window = desktop.Window()
                window.timer.stop()
                window.reply_translate.setChecked(False)
                window.auto_refresh.setChecked(False)
                window.show()
                application.processEvents()
                assert desktop.DATA_DIR == core.DATA_DIR == data_dir

                menus = [window.settings_menu, window.connection_menu, window.ubuntu_menu, window.utilities_menu, window.history_menu]
                assert window.menuBar().actions() == [menu.menuAction() for menu in menus]
                assert [menu.title() for menu in menus] == ["设置", "翻译连接", "Ubuntu 客户端", "小工具", "发送记录"]
                assert not window.findChildren(QFrame, "sidebar")
                assert window.body_splitter.width() >= window.centralWidget().width() - 90
                assert window.history_dialog is None and window.history_list is None

                assert set(window.settings_actions) == {"api", "ssh", "devices"}
                assert set(window.ubuntu_actions) == {"match", "setup", "probe", "start", "new", "projects", "settings"}
                assert set(window.connection_actions) == {stable_id, other_id}
                assert window.connection_group.isExclusive()
                assert window.manage_connections_action in window.connection_menu.actions()
                assert window.target_action in window.ubuntu_menu.actions() and not window.target_action.isEnabled()
                assert not window.connection_status_action.isEnabled()

                # The display label may change independently of the saved profile ID.
                selected = window.connection_actions[stable_id]
                assert selected.isCheckable() and selected.data() == stable_id
                assert "OpenAI-api" in selected.text()
                selected.trigger()
                application.processEvents()
                assert window.config["provider"] == stable_id
                saved = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
                assert saved["provider"] == stable_id and saved["profiles"][stable_id]["label"] == "OpenAI-api"
                assert window.connection_actions[stable_id].isChecked()
                assert not window.connection_actions[other_id].isChecked()
                assert "synthetic-openai-model" in window.connection_status_action.text()

                with patch("app.Settings") as factory:
                    factory.return_value.exec.return_value = 0
                    routes = [(window.settings_actions["api"], "api"),
                              (window.settings_actions["ssh"], "ssh"),
                              (window.settings_actions["devices"], "devices"),
                              (window.manage_connections_action, "api"),
                              (window.ubuntu_actions["settings"], "ssh")]
                    for action, section in routes:
                        factory.reset_mock()
                        action.trigger()
                        factory.assert_called_once_with(window, section)

                # These actions exercise their normal job entry points with fake SSH.
                for name in ["setup", "probe", "start", "new"]:
                    calls.clear()
                    window.ubuntu_actions[name].trigger()
                    assert window.busy and pool.pending
                    pool.drain()
                    assert name in calls and set(calls) <= {name, "capture"}
                    assert not window.busy and not window.capture_job
                window.auto_refresh.setChecked(False)

                mutating = list(window.settings_actions.values()) + list(window.connection_actions.values()) + \
                    list(window.ubuntu_actions.values()) + window.history_actions + \
                    [window.manage_connections_action, window.history_action]
                completed = []
                window.job("Synthetic foreground action", lambda: True, completed.append)
                assert window.busy
                assert all(not action.isEnabled() for action in mutating)
                assert window.source.isReadOnly() and window.english.isReadOnly()
                pool.drain()
                assert completed == [True] and not window.busy
                assert all(action.isEnabled() for action in mutating)
                assert not window.target_action.isEnabled() and not window.connection_status_action.isEnabled()

                with patch.object(window, "set_busy", wraps=window.set_busy) as busy:
                    window.refresh(quiet=True)
                    assert window.capture_job and pool.pending and not window.busy
                    assert all(action.isEnabled() for action in mutating)
                    assert not window.source.isReadOnly() and not window.english.isReadOnly()
                    assert window.refresh_button.isEnabled()
                    pool.drain()
                    assert not busy.called

                calls.clear()
                assert len(window.history) == 10 and len(window.history_actions) == 8
                assert window.history_actions[0].data()["id"] == records[-1]["id"]
                window.history_actions[0].trigger()
                application.processEvents()
                assert window.source.toPlainText() == records[-1]["source"]
                assert window.english.toPlainText() == records[-1]["english"]
                assert window.attachments == [str(attachment)] and attachment.is_file()
                assert window.draft_source == records[-1]["source"]
                assert not calls and not pool.pending

                callback_errors = []

                def run_history_dialog(choose):
                    before = (window.source.toPlainText(), window.english.toPlainText(), list(window.attachments))
                    restored = []

                    def inspect():
                        dialog = window.history_dialog
                        try:
                            assert isinstance(dialog, QDialog) and dialog.isModal()
                            assert window.history_list is not None and dialog.isAncestorOf(window.history_list)
                            assert window.history_list.count() == 10
                            assert window.history_list.currentRow() == -1 and not window.history_list.selectedItems()
                            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == before
                            if choose:
                                item = window.history_list.item(5)
                                restored.append(item.data(Qt.ItemDataRole.UserRole))
                                window.history_list.itemClicked.emit(item)
                            else:
                                dialog.reject()
                        except BaseException as error:
                            callback_errors.append(error)
                            if dialog is not None:
                                dialog.reject()

                    QTimer.singleShot(0, inspect)
                    window.history_action.trigger()
                    application.processEvents()
                    if callback_errors:
                        raise callback_errors.pop(0)
                    assert window.history_dialog is None and window.history_list is None
                    assert not calls and not pool.pending
                    return before, restored

                before, restored = run_history_dialog(False)
                assert not restored
                assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == before
                _, restored = run_history_dialog(True)
                assert len(restored) == 1
                assert window.source.toPlainText() == restored[0]["source"]
                assert window.english.toPlainText() == restored[0]["english"]
                assert window.attachments == [str(attachment)] and window.draft_source == restored[0]["source"]
                assert window.last_fingerprint == core.send_fingerprint(restored[0]["english"], restored[0]["target"], restored[0]["attachments"])

                # The real embedded devices tab remains reachable from Settings.
                def inspect_devices():
                    dialog = application.activeModalWidget()
                    try:
                        assert isinstance(dialog, desktop.Settings)
                        assert dialog.tabs.currentIndex() == 2
                        assert isinstance(dialog.devices, mobile_devices.DevicePanel)
                        assert dialog.devices.data_dir == data_dir and dialog.isAncestorOf(dialog.devices)
                    except BaseException as error:
                        callback_errors.append(error)
                    finally:
                        if isinstance(dialog, QDialog):
                            dialog.reject()

                QTimer.singleShot(0, inspect_devices)
                window.settings_actions["devices"].trigger()
                application.processEvents()
                if callback_errors:
                    raise callback_errors.pop(0)
                assert not calls and not warning.called and not translator.called and not codex.called
            finally:
                if window is not None:
                    window.timer.stop()
                    window.set_busy(False)
                    window.close()
                application.processEvents()

        print("Desktop menus UI: full-width layout, categorized routes, stable profile IDs, foreground/background guards, history restoration, and devices Settings OK")


if __name__ == "__main__":
    main()
