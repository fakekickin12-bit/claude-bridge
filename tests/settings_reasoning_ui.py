"""Reasoning settings persist and follow model capabilities without remote calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-settings-reasoning-") as directory:
        data = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data)
        os.environ["CODEX_HOME"] = str(data / "codex")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication, QWidget
        import app as desktop
        import core

        cache = data / "codex/models_cache.json"
        cache.parent.mkdir()
        cache.write_text(json.dumps({"models": [{"slug": "gpt-6.1-terra", "supported_reasoning_levels": [
            {"effort": level} for level in ("low", "medium", "high", "xhigh", "max", "ultra")]}]}), encoding="utf-8")
        codex = core.make_profile("Codex 额度", "Synthetic Codex")
        codex.update(model="gpt-6.1-terra", key_encrypted="", known_models=["gpt-6.1-terra"])
        codex.pop("reasoning_effort", None)  # Legacy connections used Low internally.
        api = core.make_profile("OpenAI", "Synthetic API")
        api.update(model="gpt-6.1-terra", reasoning_effort="medium", key_encrypted="",
                   key_env="SYNTHETIC_REASONING_UNUSED_KEY", known_models=["gpt-6.1-terra", "gpt-4.1-mini"])
        deepseek = core.make_profile("DeepSeek", "Synthetic DeepSeek")
        deepseek.update(model="deepseek-flash", reasoning_effort="low", key_encrypted="",
                        key_env="SYNTHETIC_REASONING_UNUSED_KEY", known_models=["deepseek-flash", "deepseek-v4-pro"])
        config = core.defaults()
        config.update(provider="codex-test", profiles={"codex-test": codex, "api-test": api, "deepseek-test": deepseek})
        config["ssh"]["host"] = "synthetic-reasoning-host"
        core.atomic_json(data / "config.json", config)
        qt = QApplication.instance() or QApplication([])
        qt.setStyle("Fusion")
        qt.setStyleSheet(desktop.STYLE)
        owner = QWidget()
        owner.config = json.loads(json.dumps(config))
        queued = []

        class LocalOnlySSH:
            def __init__(self, selected):
                assert selected["host"] == "synthetic-reasoning-host"

            def action(self, *_args, **_kwargs):
                raise AssertionError("Reasoning settings must not call SSH")

        def levels(dialog):
            return [dialog.reasoning.itemData(i) for i in range(dialog.reasoning.count())]

        def select(dialog, value):
            index = dialog.reasoning.findData(value)
            assert index >= 0, (value, levels(dialog))
            dialog.reasoning.setCurrentIndex(index)

        def profile(dialog, identifier):
            dialog.provider.setCurrentIndex(dialog.provider.findData(identifier))
            qt.processEvents()

        failure = AssertionError("No actual API, Codex model, or network calls")
        with patch("app.SSH", LocalOnlySSH), patch("app.Translator", side_effect=failure), \
             patch("app.CodexTranslator", side_effect=failure), patch("app.QThreadPool") as pool, \
             patch("app.protect_key", side_effect=lambda value: value), \
             patch("app.unprotect_key", side_effect=lambda value: value), \
             patch("app.QMessageBox.warning") as warning:
            pool.globalInstance.return_value.start.side_effect = queued.append
            dialog = desktop.Settings(owner)
            dialog.show()
            qt.processEvents()
            assert dialog.reasoning.isEnabled() and not dialog.reasoning.isEditable()
            assert dialog.reasoning.currentData() == "low"
            assert set(levels(dialog)) == {"", "low", "medium", "high", "xhigh", "max"}
            assert "Ultra" in dialog.reasoning_note.text() and "ultra" not in levels(dialog)

            select(dialog, "high")
            profile(dialog, "api-test")
            assert dialog.reasoning.currentData() == "medium"
            assert {"none", "max"} <= set(levels(dialog))
            profile(dialog, "codex-test")
            assert dialog.reasoning.currentData() == "high", "Each connection keeps its own selection"
            dialog.save()
            assert not warning.called and dialog.result() == desktop.QDialog.DialogCode.Accepted
            owner.config = core.load_config(data / "config.json")
            assert owner.config["profiles"]["codex-test"]["reasoning_effort"] == "high"
            restored = desktop.Settings(owner)
            restored.show()
            qt.processEvents()
            assert restored.reasoning.currentData() == "high"

            profile(restored, "deepseek-test")
            assert set(levels(restored)) == {"", "none", "low", "high", "max"}
            assert restored.reasoning.currentData() == "low"
            restored.model.setCurrentText("deepseek-v4-pro")
            assert set(levels(restored)) == {"", "none", "low", "high", "max"}
            assert not {"medium", "xhigh", "ultra"} & set(levels(restored))
            select(restored, "none")
            profile(restored, "codex-test")
            assert restored.reasoning.currentData() == "high"
            profile(restored, "deepseek-test")
            assert restored.reasoning.currentData() == "none"

            profile(restored, "api-test")
            select(restored, "high")
            restored.model.setCurrentText("")
            assert not restored.reasoning.isEnabled() and restored.reasoning.currentData() == ""
            restored.model.setCurrentText("gpt-6")
            assert not restored.reasoning.isEnabled()
            restored.model.setCurrentText("gpt-6.1-terra")
            assert restored.reasoning.currentData() == "high", "Transient typing must preserve valid chosen effort"

            restored.models_button.click()
            assert len(queued) == 1
            queued.pop().signals.done.emit(["gpt-4.1-mini", "gpt-6.1-terra"])
            qt.processEvents()
            assert restored.reasoning.currentData() == "high", "Model-list refresh must not reset reasoning"
            assert restored.model.currentText() == "gpt-6.1-terra"
            restored.model.setCurrentText("gpt-4.1-mini")
            assert not restored.reasoning.isEnabled()
            assert levels(restored) == [""] and restored.reasoning.currentData() == ""
            assert "默认" in restored.reasoning_note.text()
            assert not queued and not warning.called
            restored.reject()
            owner.close()
            qt.processEvents()
        print("Settings reasoning UI: legacy Low, High save/reload, model-aware levels, per-profile choices, typed/list refresh preservation, no remote calls OK")


if __name__ == "__main__":
    main()
