"""Desktop Settings/device enrollment integration using temporary local data only."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def settle(application, predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Settings/device UI did not reach its expected state")


def snapshot(database):
    with sqlite3.connect(database) as connection:
        return {
            "jobs": connection.execute("SELECT * FROM jobs ORDER BY id").fetchall(),
            "devices": connection.execute("SELECT * FROM devices ORDER BY id").fetchall(),
            "pairings": connection.execute("SELECT * FROM device_pairings ORDER BY code_hash").fetchall(),
        }


def main():
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="bridge-settings-devices-") as directory:
        data_dir = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data_dir)
        os.environ["CLAUDE_BRIDGE_ORIGIN"] = "https://bridge.example.test"
        os.environ["PYTHONPATH"] = str(source)
        sys.path.insert(0, str(source))

        # A fresh interpreter catches accidental web-runtime imports, including
        # those that a prior UI test process could already have cached.
        check = (
            "import app,mobile_devices,device_auth,sys; "
            "assert not any(name.split('.')[0] in {'fastapi','uvicorn'} for name in sys.modules), "
            "'Desktop enrollment imported the web server runtime'"
        )
        subprocess.run([sys.executable, "-c", check], cwd=source, env=os.environ.copy(),
                       capture_output=True, text=True, check=True)

        from PySide6.QtWidgets import QApplication, QPushButton
        from PySide6.QtCore import QCoreApplication, QEvent
        from shiboken6 import isValid
        import app as desktop
        import core
        import device_auth
        import mobile_devices

        config = core.defaults()
        config["provider"] = next(iter(config["profiles"]))
        config["auto_translate_reply"] = False
        config["ssh"]["host"] = "synthetic-settings-host"
        for profile in config["profiles"].values():
            profile["key_env"] = "SYNTHETIC_SETTINGS_UNUSED_API_KEY"
        core.atomic_json(data_dir / "config.json", config)

        now = time.time()
        with device_auth.device_database(data_dir) as connection:
            connection.execute("""CREATE TABLE jobs (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, phase TEXT NOT NULL, result TEXT, error TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)", (
                "a" * 32, "synthetic-fingerprint", "{}", "running", "Synthetic running job",
                None, "", now, now))
            connection.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?)", (
                "b" * 32, hashlib.sha256(b"synthetic-device-token").hexdigest(), "synthetic-csrf",
                "Synthetic registered phone", now, now, now + device_auth.DEVICE_TTL, 0))
        database = data_dir / "mobile" / "bridge.sqlite3"
        original = snapshot(database)

        application = QApplication.instance() or QApplication([])
        window = None
        dialogs = []
        failure = AssertionError("This UI check must not call SSH or a model")
        with patch("app.SSH", side_effect=failure) as ssh, \
             patch("app.Translator", side_effect=failure) as api_translator, \
             patch("app.CodexTranslator", side_effect=failure) as codex_translator, \
             patch("app.QMessageBox.warning") as warning, \
             patch("mobile_devices.QMessageBox.warning") as device_warning, \
             patch("mobile_devices.issue_pairing_code", wraps=device_auth.issue_pairing_code) as issue:
            try:
                window = desktop.Window()
                window.timer.stop()
                window.reply_translate.setChecked(False)
                window.auto_refresh.setChecked(False)
                window.show()
                application.processEvents()
                assert core.DATA_DIR == desktop.DATA_DIR == data_dir

                menu = window.settings_menu
                action = window.settings_actions["devices"]
                assert menu.menuAction() in window.menuBar().actions()
                assert menu.title() == "设置" and action in menu.actions()
                assert action.text() == "手机 / iPad 配对"
                with patch("app.Settings") as factory:
                    factory.return_value.exec.return_value = 0
                    action.trigger()
                    factory.assert_called_once_with(window, "devices")

                first = desktop.Settings(window, "devices")
                dialogs.append(first)
                panel = first.devices
                assert first.tabs.count() == 3 and first.tabs.tabText(2) == "手机 / iPad 配对"
                assert first.tabs.currentIndex() == 2 and first.tabs.widget(2) is panel
                assert isinstance(panel, mobile_devices.DevicePanel)
                assert first.isAncestorOf(panel) and not panel.isWindow()
                assert panel.data_dir == data_dir and not panel.timer.isActive()
                first.show()
                settle(application, lambda: panel.isVisible() and panel.timer.isActive())
                assert panel.table.rowCount() == 1 and issue.call_count == 0

                pair = panel.findChild(QPushButton, "pair")
                assert pair is not None
                pair.click()
                application.processEvents()
                assert issue.call_count == 1 and not device_warning.called
                note = data_dir / "mobile" / "设备配对信息.txt"
                note_bytes = note.read_bytes()
                note_mtime = note.stat().st_mtime_ns
                displayed_note = panel.note.toPlainText()
                if "配对码：" not in displayed_note or not note_bytes:
                    raise AssertionError("Generating a temporary pairing code did not display its note")
                pixmap = panel.qr.pixmap()
                assert pixmap is not None and not pixmap.isNull()
                first_qr = pixmap.toImage()
                generated = snapshot(database)
                assert generated["jobs"] == original["jobs"]
                assert generated["devices"] == original["devices"]
                assert len(generated["pairings"]) == 1

                first.tabs.setCurrentIndex(0)
                settle(application, lambda: not panel.isVisible() and not panel.timer.isActive())
                first.tabs.setCurrentIndex(2)
                settle(application, lambda: panel.isVisible() and panel.timer.isActive())
                first.hide()
                settle(application, lambda: not panel.timer.isActive())
                first.show()
                settle(application, lambda: panel.timer.isActive())
                first.close()
                settle(application, lambda: not panel.timer.isActive())

                reopened = desktop.Settings(window, "devices")
                dialogs.append(reopened)
                reopened.show()
                settle(application, lambda: reopened.devices.timer.isActive())
                restored = reopened.devices
                if restored.note.toPlainText() != displayed_note or note.read_bytes() != note_bytes:
                    raise AssertionError("Reopening Settings changed the existing temporary pairing code")
                assert restored.qr.pixmap().toImage() == first_qr
                assert note.stat().st_mtime_ns == note_mtime and issue.call_count == 1
                assert restored.table.rowCount() == 1 and snapshot(database) == generated
                reopened.close()
                settle(application, lambda: not restored.timer.isActive())
                reopened.show()
                settle(application, lambda: restored.timer.isActive())
                reopened.tabs.setCurrentIndex(0)
                settle(application, lambda: not restored.timer.isActive())
                assert snapshot(database) == generated and issue.call_count == 1

                # Completion closures must remain harmless after the Settings
                # QObject and its controls have actually been destroyed.
                for method in ("fetch_models", "check_codex"):
                    for outcome in ("done", "failed"):
                        pending = desktop.Settings(window)
                        pending.show()
                        application.processEvents()
                        queued = []
                        with patch("app.QThreadPool") as pool:
                            pool.globalInstance.return_value.start.side_effect = queued.append
                            getattr(pending, method)()
                        assert len(queued) == 1 and pending.jobs == queued
                        job = queued[0]
                        pending.reject()
                        assert not pending.active_dialog
                        pending.deleteLater()
                        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                        application.processEvents()
                        assert not isValid(pending) and not isValid(pending.info)
                        value = "Synthetic late failure" if outcome == "failed" else (
                            ["synthetic-model"] if method == "fetch_models" else {"text": "Synthetic login status"})
                        with patch("sys.excepthook") as exception_hook:
                            getattr(job.signals, outcome).emit(value)
                            application.processEvents()
                            assert not exception_hook.called
                        assert not pending.jobs

                assert not warning.called and not device_warning.called
                assert not ssh.called and not api_translator.called and not codex_translator.called
                assert not any(name.split(".")[0] in {"fastapi", "uvicorn"} for name in sys.modules)
            finally:
                for dialog in dialogs:
                    dialog.close()
                if window is not None:
                    window.close()
                application.processEvents()
        print("Settings devices UI: menu routing, shared data, QR persistence, timer lifecycle, queue/device preservation, and late callbacks OK")


if __name__ == "__main__":
    main()
