"""Desktop receipt recovery with temporary files, fake SSH, and no model/UI input."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication
import app as desktop
import core


CONTEXT = "a" * 64


class Pool:
    def start(self, job):
        job.run()


class Remote:
    def __init__(self):
        self.calls = []
        self.clients = []
        self.send_error = core.BridgeError("Synthetic SSH timeout")
        self.receipt_state = "missing"
        self.window = None
        self.read_checks = False

    def client(self, target):
        target = dict(target)
        self.clients.append(target)
        remote = self

        class Client:
            def action(self, action, **kwargs):
                remote.calls.append((action, kwargs, target))
                if action == "check_ready":
                    return {"ready": True, "context": CONTEXT, "mode": "Code"}
                if action == "send":
                    assert kwargs["timeout"] == 180
                    if remote.send_error:
                        raise remote.send_error
                    return {"state": "submitted"}
                if action == "delivery_status":
                    assert set(kwargs) == {"request_id", "fingerprint", "timeout"}
                    assert kwargs["timeout"] == 25
                    if remote.read_checks:
                        assert not remote.window.busy and not remote.window.source.isReadOnly()
                        assert remote.window.settings_actions["api"].isEnabled()
                    if remote.receipt_state == "missing":
                        return {"found": False, "matched": False, "request_id": kwargs["request_id"], "completed": False}
                    enter = remote.receipt_state != "pasted-only"
                    state = "pasted" if remote.receipt_state.startswith("pasted") else "submitted"
                    return {"found": True, "matched": True, "request_id": kwargs["request_id"],
                            "fingerprint": kwargs["fingerprint"], "state": state,
                            "auto_enter": enter, "context": CONTEXT,
                            "completed": state == "submitted" or not enter}
                if action == "capture":
                    return {"text": "Synthetic reply", "mode": "Code", "generating": False}
                raise AssertionError("Receipt recovery must never navigate or call a model: " + action)
        return Client()

    def count(self, action):
        return sum(name == action for name, _, _ in self.calls)


def prepare(window):
    window.source.setPlainText("检查合成任务")
    window.english.setPlainText("Inspect the synthetic task.")
    window.draft_source = window.source.toPlainText()


def stop(window):
    for timer in (window.timer, window.delivery_timer, window.draft_timer, window.layout_timer):
        timer.stop()


def main():
    qt = QApplication.instance() or QApplication([])
    pool = Pool()
    warnings = []
    with tempfile.TemporaryDirectory(prefix="bridge-desktop-delivery-") as directory:
        root = Path(directory)
        for name in ("immediate", "late", "changed", "pasted", "pasted-auto", "history-error", "restart"):
            data = root / name
            config = core.defaults()
            config["ssh"]["host"] = "synthetic-host"
            config["auto_translate_reply"] = False
            remote = Remote()
            with patch.object(desktop, "DATA_DIR", data), patch.object(core, "DATA_DIR", data), \
                 patch.object(desktop, "load_config", return_value=copy.deepcopy(config)), \
                 patch.object(desktop, "SSH", side_effect=remote.client), \
                 patch.object(desktop.QThreadPool, "globalInstance", return_value=pool), \
                 patch.object(desktop.QMessageBox, "warning", side_effect=lambda *args: warnings.append(args)):
                window = desktop.Window()
                remote.window = window
                stop(window)
                prepare(window)
                before = (window.source.toPlainText(), window.english.toPlainText(), list(window.attachments))
                if name == "immediate":
                    remote.receipt_state = "submitted"
                if name == "pasted":
                    window.enter.setChecked(False)
                    remote.receipt_state = "pasted-only"
                if name == "pasted-auto":
                    remote.receipt_state = "pasted-auto"
                if name == "history-error":
                    remote.send_error = None
                    original = desktop.history_record
                    def fail_confirmed(*args, **kwargs):
                        if args[4] == "submitted":
                            raise OSError("Synthetic local history failure")
                        return original(*args, **kwargs)
                    with patch.object(desktop, "history_record", side_effect=fail_confirmed):
                        window.send()
                    assert "已发送" in window.badge.text()
                    assert "记录写入失败" in window.upload_status.text()
                    assert not window.source.toPlainText() and not window.pending_deliveries
                    assert remote.count("send") == 1 and remote.count("delivery_status") == 0
                    stop(window)
                    window.close()
                    continue
                window.send()
                assert not window.busy and remote.count("send") == 1
                assert remote.count("delivery_status") == 1
                if name == "immediate":
                    assert not window.source.toPlainText() and not window.english.toPlainText()
                    assert window.history[0]["state"] == "submitted" and not window.pending_deliveries
                elif name == "pasted":
                    assert window.history[0]["state"] == "pasted"
                    assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == before
                    assert not window.pending_deliveries
                else:
                    assert window.history[0]["state"] == "unconfirmed" and window.pending_deliveries
                    assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == before
                    record = next(iter(window.pending_deliveries.values()))
                    saved = json.loads((data / "history" / (record["id"] + ".json")).read_text(encoding="utf-8"))
                    assert saved["delivery"]["fingerprint"] and saved["delivery"]["local_paths"] == []
                    count = remote.count("send")
                    window.send()
                    assert remote.count("send") == count, "An uncertain draft must not be resubmitted"
                    if name == "pasted-auto":
                        window.poll_deliveries()
                        assert window.pending_deliveries and remote.count("send") == count
                    else:
                        if name == "restart":
                            stop(window)
                            window.close()
                            window = desktop.Window()
                            remote.window = window
                            stop(window)
                            assert record["id"] in window.pending_deliveries
                            assert window.english.toPlainText() == before[1]
                            window.config["ssh"]["host"] = "synthetic-other-host"
                        if name == "changed":
                            window.source.setPlainText("新的合成草稿")
                            window.english.setPlainText("A new draft.")
                            attachment = data / "new.txt"
                            attachment.parent.mkdir(parents=True, exist_ok=True)
                            attachment.write_text("synthetic", encoding="utf-8")
                            window.add_attachments([str(attachment)])
                            edited = (window.source.toPlainText(), window.english.toPlainText(), list(window.attachments))
                        remote.receipt_state = "submitted"
                        remote.read_checks = True
                        window.poll_deliveries()
                        assert not window.pending_deliveries and not window.delivery_job
                        assert remote.count("send") == count
                        assert window.history[0]["state"] == "submitted"
                        if name == "changed":
                            assert (window.source.toPlainText(), window.english.toPlainText(), window.attachments) == edited
                        else:
                            assert not window.source.toPlainText() and not window.english.toPlainText()
                        reads = [target for action, _, target in remote.calls if action == "delivery_status"]
                        assert all(target["host"] == "synthetic-host" for target in reads)
                stop(window)
                window.close()
                qt.processEvents()

    identity = "b" * 64
    valid = {"found": True, "matched": True, "completed": True, "fingerprint": identity,
             "state": "submitted", "auto_enter": True}
    assert desktop.confirmed_delivery(valid, identity, True) == "submitted"
    for change in ({"found": 1}, {"matched": "true"}, {"completed": False}, {"fingerprint": "c" * 64},
                   {"state": "submitting"}, {"state": "pasted", "auto_enter": True}):
        assert desktop.confirmed_delivery({**valid, **change}, identity, True) is None
    assert desktop.confirmed_delivery({**valid, "state": "pasted", "auto_enter": False}, identity, False) == "pasted"
    print("Desktop delivery UI: timeout receipt recovery, strict identities, no resubmission, draft preservation, restart recovery, and history-failure success OK")


if __name__ == "__main__":
    main()
