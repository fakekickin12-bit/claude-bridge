"""The desktop menu opens only the operator's web entry, with no remote actions."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-desktop-web-menu-") as directory:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = directory
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication
        import app as desktop
        qt = QApplication.instance() or QApplication([])
        with patch.object(desktop, "SSH", side_effect=AssertionError("No native action from web menu")), \
             patch.object(desktop.QDesktopServices, "openUrl", return_value=True) as launch, \
             patch.object(desktop, "pairing_url", return_value="https://bridge.example.test"):
            window = desktop.Window()
            window.timer.stop(); window.delivery_timer.stop()
            window.source.setPlainText("Synthetic retained draft")
            window.desktop_web_action.trigger()
            assert launch.call_args[0][0].toString() == "https://bridge.example.test/desktop"
            assert window.source.toPlainText() == "Synthetic retained draft" and not window.busy
            with patch.object(desktop, "pairing_url", side_effect=desktop.BridgeError("Configure own HTTPS origin")), \
                 patch.object(desktop.QMessageBox, "information") as message:
                window.desktop_web_action.trigger()
                assert launch.call_count == 1 and message.call_count == 1
            window.close()
            qt.processEvents()
    print("Desktop web menu: operator origin, separate browser page, retained draft, no SSH/model calls OK")


if __name__ == "__main__":
    main()
