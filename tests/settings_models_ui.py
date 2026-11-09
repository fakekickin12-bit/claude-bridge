"""Model selection and stale discovery checks using temporary, synthetic settings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-settings-models-") as directory:
        data = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QWidget
        import app as desktop
        import core

        qt = QApplication.instance() or QApplication([])
        qt.setStyle("Fusion")
        qt.setStyleSheet(desktop.STYLE)
        profile = core.make_profile("DeepSeek", "Synthetic connection")
        profile.update(base_url="https://models.example.invalid", model="synthetic-first",
                       key_env="SYNTHETIC_MODELS_UNUSED_KEY", key_encrypted="",
                       known_models=["synthetic-first", "synthetic-second"])
        config = core.defaults()
        config["provider"] = "synthetic-profile"
        config["profiles"] = {"synthetic-profile": profile}
        config["ssh"]["host"] = "synthetic-models-host"
        core.atomic_json(data / "config.json", config)
        owner = QWidget()
        owner.config = json.loads(json.dumps(config))
        queued, calls = [], []

        class FakeTranslator:
            def __init__(self, selected, key):
                assert selected["base_url"] == "https://models.example.invalid"
                assert key == ""

            def models(self):
                calls.append("models")
                return ["synthetic-first", "synthetic-second"]

        class LocalOnlySSH:
            def __init__(self, selected):
                assert selected["host"] == "synthetic-models-host"

            def action(self, *_args, **_kwargs):
                raise AssertionError("Settings selection must not call SSH")

        def finish():
            queued.pop(0).run()
            qt.processEvents()

        with patch("app.QThreadPool") as pool, patch("app.Translator", FakeTranslator), \
             patch("app.CodexTranslator", side_effect=AssertionError("No real model calls")), \
             patch("app.SSH", LocalOnlySSH), patch("app.protect_key", side_effect=lambda value: value), \
             patch("app.unprotect_key", side_effect=lambda value: value), \
             patch("app.QMessageBox.warning") as warning:
            pool.globalInstance.return_value.start.side_effect = queued.append
            dialog = desktop.Settings(owner)
            dialog.show()
            qt.processEvents()
            assert dialog.model.isEditable()
            dialog.models_button.click()
            assert len(queued) == 1 and not dialog.models_button.isEnabled()
            finish()
            assert calls == ["models"]
            assert [dialog.model.itemText(i) for i in range(dialog.model.count())] == ["synthetic-first", "synthetic-second"]
            assert dialog.model.currentText() == "synthetic-first"
            dialog.model.showPopup()
            qt.processEvents()
            QTest.keyClick(dialog.model.view(), Qt.Key.Key_Down)
            QTest.keyClick(dialog.model.view(), Qt.Key.Key_Return)
            qt.processEvents()
            assert dialog.model.currentText() == "synthetic-second"
            dialog.save()
            assert not warning.called and dialog.result() == desktop.QDialog.DialogCode.Accepted
            owner.config = core.load_config(data / "config.json")
            restored = desktop.Settings(owner)
            assert restored.model.currentText() == "synthetic-second"
            assert restored.model.count() == 2

            # Text edited while the list request is in flight stays selected.
            restored.models_button.click()
            restored.model.setCurrentText("manually-entered-model")
            finish()
            assert restored.model.currentText() == "manually-entered-model"
            assert restored.models_button.isEnabled() and restored.provider.isEnabled()

            # New/duplicated connections can still be created while discovery runs.
            restored.models_button.click()
            old_profile = restored.active
            restored.add_profile(True)
            assert restored.active != old_profile
            restored.model.clear()
            restored.model.addItem("new-connection-only")
            restored.model.setCurrentText("new-connection-only")
            finish()
            assert restored.model.currentText() == "new-connection-only"
            assert restored.model.count() == 1 and "已忽略旧模型列表" in restored.info.text()
            assert restored.models_button.isEnabled() and restored.provider.isEnabled()

            # Same connection ID with a changed endpoint also rejects old discovery.
            restored.models_button.click()
            restored.base.setText("https://changed.example.invalid")
            late_job = queued.pop(0)
            late_job.signals.done.emit(["old-endpoint-only"])
            qt.processEvents()
            assert restored.model.currentText() == "new-connection-only"
            assert restored.model.count() == 1 and "已忽略旧模型列表" in restored.info.text()
            assert restored.models_button.isEnabled() and restored.provider.isEnabled()

            # A provider-template reset under the same profile ID is also a new source.
            restored.base.setText("https://models.example.invalid")
            restored.models_button.click()
            restored.template.setCurrentText("自定义兼容 API")
            restored.model.setCurrentText("new-template-model")
            late_job = queued.pop(0)
            late_job.signals.failed.emit("old-provider-error")
            qt.processEvents()
            assert restored.model.currentText() == "new-template-model"
            assert "old-provider-error" not in restored.info.text() and "已忽略旧模型列表" in restored.info.text()
            assert restored.models_button.isEnabled() and restored.provider.isEnabled()

            # Removing the requesting connection must not populate its replacement.
            restored.template.setCurrentText("DeepSeek")
            restored.base.setText("https://models.example.invalid")
            restored.models_button.click()
            removed = restored.active
            with patch("app.QMessageBox.question", return_value=desktop.QMessageBox.StandardButton.Yes):
                restored.remove_profile()
            assert restored.active != removed and removed not in restored.config["profiles"]
            replacement = restored.model.currentText()
            late_job = queued.pop(0)
            late_job.signals.done.emit(["removed-connection-only"])
            qt.processEvents()
            assert restored.model.currentText() == replacement
            assert restored.model.findText("removed-connection-only") < 0
            assert restored.models_button.isEnabled() and restored.provider.isEnabled()
            assert not queued and not restored.jobs
            restored.reject()
            owner.close()
            qt.processEvents()
        print("Settings models UI: dropdown selection/save/reload, manual entry preservation, stale profile/endpoint/template discovery rejection, no SSH/network OK")


if __name__ == "__main__":
    main()
