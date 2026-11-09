"""Read-only delivery receipts with synthetic ledgers and no UI or network."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d
import remote as r


def rejected(request):
    try:
        d.delivery_status(request)
    except d.DesktopError:
        return
    raise AssertionError("An invalid delivery lookup was accepted")


with tempfile.TemporaryDirectory(prefix="bridge-delivery-receipt-") as directory:
    root = Path(directory)
    rid, fingerprint = "a" * 32, "b" * 64
    request = {"request_id": rid, "fingerprint": fingerprint}
    ledger = root / "desktop-deliveries" / (rid + ".json")
    with patch.object(d, "ROOT", root), patch.object(r, "ROOT", root), \
         patch.object(d, "status", side_effect=AssertionError("Receipt read touched the UI")) as status, \
         patch.object(d, "document", side_effect=AssertionError("Receipt read inspected a page")) as document, \
         patch.object(d, "send", side_effect=AssertionError("Receipt read sent a message")) as send, \
         patch.object(d, "app", side_effect=AssertionError("Receipt read accessed an app")) as application:
        absent = d.delivery_status(request)
        assert absent == {"found": False, "matched": False, "request_id": rid, "completed": False}
        for key, invalid in [("request_id", "../ledger"), ("request_id", "a" * 31), ("request_id", 12),
                             ("fingerprint", "z" * 64), ("fingerprint", "b" * 63), ("fingerprint", None)]:
            rejected({**request, key: invalid})
        record = {**request, "state": "submitted", "context": "c" * 64, "auto_enter": True,
                  "text": "Synthetic private prompt", "attachment_paths": ["/synthetic/private/file.png"],
                  "workspace": "/synthetic/private/project", "native_labels": ["private-file.png"],
                  "unexpected": {"api_key": "synthetic-private-value"}}
        r.atomic(ledger, record)
        matched = d.delivery_status(request)
        assert matched == {"found": True, "matched": True, **request, "state": "submitted",
                           "context": "c" * 64, "auto_enter": True, "completed": True}
        assert d.delivery_status({**request, "fingerprint": "d" * 64}) == absent
        serialized = json.dumps(matched)
        assert all(value not in serialized for value in ("Synthetic private prompt", "/synthetic", "private-file", "api_key"))
        for state, auto_enter, completed in [("preparing", True, False), ("submitting", True, False),
                                              ("pasted", True, False), ("pasted", False, True),
                                              ("submitted", True, True), ("submitted", False, True),
                                              ("cleared", False, False)]:
            r.atomic(ledger, {**record, "state": state, "auto_enter": auto_enter})
            result = d.delivery_status(request)
            assert result["completed"] is completed and result["state"] == state
        r.atomic(ledger, {key: value for key, value in {**record, "state": "pasted"}.items() if key != "auto_enter"})
        assert d.delivery_status(request)["completed"] is False
        assert d.delivery_status(request)["auto_enter"] is None
        r.atomic(ledger, {**record, "request_id": "e" * 32})
        assert d.delivery_status(request) == absent
        r.atomic(ledger, {**record, "state": ["submitted"], "context": "/private/path", "fingerprint": fingerprint})
        invalid = d.delivery_status(request)
        assert invalid["state"] == "unknown" and invalid["context"] is None and not invalid["completed"]
        r.atomic(ledger, {**record, "fingerprint": "not-a-fingerprint"})
        assert d.delivery_status(request) == absent
        r.atomic(ledger, record)
        result = r.dispatch({"transport": "desktop", "action": "delivery_status", **request})
        assert result["state"] == "submitted" and result["completed"] is True
        assert not status.called and not document.called and not send.called and not application.called
print("Delivery status: owned hash match, absent/invalid/mismatched lookup, intermediate pasted state, and zero UI/send calls OK")
