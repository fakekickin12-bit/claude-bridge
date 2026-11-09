"""A clean installation must not contact any remote service before configuration."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-first-run-") as directory:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = directory
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication
        import app as desktop

        application = QApplication.instance() or QApplication([])
        failure = AssertionError("Fresh install attempted a remote operation")
        with patch("app.SSH", side_effect=failure) as ssh, \
             patch("app.Translator", side_effect=failure) as api, \
             patch("app.CodexTranslator", side_effect=failure) as codex:
            assert not (Path(directory) / "config.json").exists()
            window = desktop.Window()
            window.show()
            application.processEvents()
            assert window.connection_configured is False
            assert not window.config.get("auto_enter"), "Sharing build must not submit by default"
            window.auto_refresh.setChecked(True)
            window.reply_translate.setChecked(True)
            for _ in range(4):
                window.refresh(quiet=True)
                window.poll_deliveries()
                application.processEvents()
            assert not ssh.called and not api.called and not codex.called
            assert not window.reply_original
            assert window.connection_configured is False
            for timer in (window.timer, window.delivery_timer, window.layout_timer, window.draft_timer):
                timer.stop()
            window.close()
            application.processEvents()
        print("First run UI: no inherited keys, no remote/model calls, no automatic submission OK")


if __name__ == "__main__":
    main()
