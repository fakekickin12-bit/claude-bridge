"""Current attachment capability is checked before upload; failed drafts stay editable."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-attachment-support-ui-") as directory:
        data = Path(directory)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["CLAUDE_BRIDGE_DATA"] = str(data)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from PySide6.QtWidgets import QApplication
        import app as desktop
        import core

        config = core.defaults()
        config["auto_translate_reply"] = False
        core.atomic_json(data / "config.json", config)
        qt = QApplication.instance() or QApplication([])
        capabilities = {"native_attachments": False,
                        "attachment_reason": "当前窗口未开放文件拖入接口，请在 Ubuntu 手动添加附件。"}
        calls, warnings = [], []
        runtime = {"busy": False}
        context, batch = "a" * 64, "b" * 32

        class Remote:
            def __init__(self, target): pass
            def action(self, action, **payload):
                calls.append((action, payload))
                if action == "check_ready":
                    assert payload.get("for_send") is True
                    return {"ready": True, "mode": "Code", "context": context, "busy": runtime["busy"],
                            "capabilities": dict(capabilities)}
                assert action == "send", "No translation, navigation, receipt polling or implicit retries"
                assert payload["attachment_batch"] == batch and payload["enter"] is False
                assert payload["context"] == context and payload["text"].startswith("Inspect this screenshot.")
                return {"state": "pasted"}

        class Pool:
            def start(self, job): job.run()

        path = data / "test.png"
        path.write_bytes(b"synthetic attachment bytes")
        original = {"id": "c" * 32, "path": str(path), "name": path.name, "kind": "image",
                    "size": path.stat().st_size, "sha256": "d" * 64}
        bundle = {"batch_id": batch, "originals": [original], "files": []}
        uploaded = {"local_path": str(path), "name": path.name, "kind": "image", "size": original["size"],
                    "sha256": original["sha256"], "remote_path": "/owned-batch/test.png"}
        with patch("app.SSH", Remote), patch("app.QThreadPool.globalInstance", return_value=Pool()), \
             patch.object(desktop.Window, "refresh"), \
             patch("app.Translator", side_effect=AssertionError("Do not retranslate the user's English draft")), \
             patch("app.CodexTranslator", side_effect=AssertionError("No model calls")), \
             patch("app.build_bundle", return_value=bundle) as build, \
             patch("app.upload_bundle", return_value=[uploaded]) as upload, \
             patch("app.QMessageBox.warning", side_effect=lambda *values: warnings.append(values)):
            window = desktop.Window()
            for timer in (window.timer, window.delivery_timer, window.layout_timer, window.draft_timer): timer.stop()
            window.source.setPlainText("检查这张合成截图")
            window.english.setPlainText("Inspect this screenshot.")
            window.add_attachments([str(path)])
            window.enter.setChecked(False)
            before = (window.source.toPlainText(), window.english.toPlainText(), list(window.attachments))
            window.send()
            qt.processEvents()
            assert window.upload_status.text() == capabilities["attachment_reason"]
            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == before
            assert not build.called and not upload.called
            assert [name for name, _ in calls] == ["check_ready"]
            assert window.history[0]["state"] == "not_sent" and not window.pending_deliveries
            capabilities.clear()
            capabilities["native_attachments"] = True
            capabilities["running_attachments"] = False
            runtime["busy"] = True
            window.send()
            qt.processEvents()
            assert "可追加文字指令" in window.upload_status.text()
            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == before
            assert not build.called and not upload.called
            runtime["busy"] = False
            window.send()
            qt.processEvents()
            assert [name for name, _ in calls] == ["check_ready", "check_ready", "check_ready", "send"]
            assert build.call_count == upload.call_count == 1
            assert window.history[0]["state"] == "pasted" and not window.pending_deliveries
            assert not window.busy
            window.close()
            qt.processEvents()
        print("Attachment availability UI: fresh reason before upload, draft preservation, explicit retry, attached batch and no translation/resubmission OK")


if __name__ == "__main__":
    main()
