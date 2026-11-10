"""Running Code input is isolated, witnessed and never replayed. Synthetic UI only."""
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d
import remote as r


def rejected(call):
    try:
        call()
    except d.DesktopError as error:
        return error
    raise AssertionError("An unsafe running send was accepted")


context, pid = "a" * 64, 42
initial = {"context": context, "mode": "Code", "title": "Synthetic Code", "process_id": pid, "new_session": False,
           "ready": True, "busy": True, "allow_running": True, "capabilities": {"native_attachments": True}}
current = {**initial, "draft": "", "attachments": [], "dialogs": [], "approval": False,
           "awaiting_input": False, "locked": False, "window_visible": True, "attachment_pending": False}
busy_state = {**current, "ready": False, "input_ready": False, "send_ready": True, "send_input_ready": True,
              "send_reason": "Can append", "reason": "Running", "send_mode": "running_insert", "running_input_capable": True}
with tempfile.TemporaryDirectory(prefix="bridge-running-new-") as guard_directory, \
     patch.object(d, "ROOT", Path(guard_directory)), \
     patch.object(d, "status", return_value=busy_state), patch.object(d, "window_id", return_value=100), \
     patch.object(d, "find") as navigate:
    rejected(lambda: d.check_ready({"context": context}))
    ready = d.check_ready({"context": context, "for_send": True})
    assert ready["ready"] and ready["allow_running"] and ready["busy"]
    rejected(lambda: d.dispatch({"action": "new", "context": context, "for_send": True}))
    navigate.assert_not_called()
with patch.object(d, "status", return_value={**busy_state, "send_ready": False, "send_input_ready": False}):
    rejected(lambda: d.check_ready({"context": context, "for_send": True}))
rejected(lambda: d.require_attachment_draft(current, context, [], ""))
assert d.require_attachment_draft(current, context, [], "", allow_running=True) == {}
rejected(lambda: d.require_attachment_draft({**current, "attachments": ["one.png"]}, context,
                                           [str(Path.home()/"one.png")], "", allow_running=True))
for change in ({"approval": True}, {"awaiting_input": True}, {"locked": True}, {"window_visible": False},
               {"context": "b" * 64}, {"draft": "User draft"}, {"dialogs": ["Open File"]}):
    rejected(lambda: d.require_attachment_draft({**current, **change}, context, [], "", allow_running=True))


class Node:
    serial = 0
    def __init__(self, role="section", name="", attrs=(), children=(), forbid_name=False):
        Node.serial += 1
        self.path = "/org/a11y/atspi/accessible/running_" + str(Node.serial)
        self.role, self._name, self.attrs, self.children = role, name, list(attrs), list(children)
        self.forbid_name = forbid_name
    def getRoleName(self): return self.role
    def getAttributes(self): return self.attrs
    @property
    def name(self):
        if self.forbid_name: raise AssertionError("An older body was read")
        return self._name
    @property
    def childCount(self): return len(self.children)
    def getChildAtIndex(self, index): return self.children[index]


def human(value, forbid=False):
    return Node("article", children=[Node("heading", "You said: test", ["class:sr-only"]),
        Node(attrs=["class:group/bubble"], children=[Node("paragraph", children=[Node("static", value, forbid_name=forbid)])])])


older, latest = human("Older private body", True), human("Previous prompt", True)
feed = Node("panel", "Chat messages", ["xml-roles:feed"], [Node(children=[older, latest])])
page = Node("document web", children=[feed])
baseline = d.running_user_snapshot(page)
assert baseline == {"feed_id": feed.path, "message_id": latest.path}
value = "追加这一条\nKeep the original task running."
new = human(value)
feed.children[0].children.append(new)
assert d.running_user_snapshot(page, read_text=True)["text"] == value
application = SimpleNamespace(get_process_id=lambda: pid)
expected_hash = d.normalized_message_hash(value)
with patch.object(d, "app", return_value=application), patch.object(d, "document", return_value=(page, {})), \
     patch.object(d, "page_context", return_value=context), patch.object(d, "code_panel", return_value=page):
    proof = d.running_submission_confirmed(initial, current, expected_hash, baseline)
    assert proof["kind"] == "new_user_message" and len(proof["message_id_hash"]) == 64
    assert d.running_submission_confirmed(initial, {**current, "busy": False}, expected_hash, baseline)
    assert not d.running_submission_confirmed(initial, current, expected_hash, {**baseline, "message_id": new.path})
    assert not d.running_submission_confirmed(initial, current, d.normalized_message_hash("Different message"), baseline)
    assert not d.running_submission_confirmed(initial, {**current, "draft": value}, expected_hash, baseline)
    rejected(lambda: d.running_submission_confirmed(initial, {**current, "context": "b" * 64}, expected_hash, baseline))

# Moving focus to another control during the slow status scan must prevent the
# Return fallback even while the same Claude window remains active.
focus = {"present": True}
entry = SimpleNamespace(name="Prompt", getRoleName=lambda: "entry", getAttributes=lambda: ["class:tiptap ProseMirror"],
                        getState=lambda: SimpleNamespace(contains=lambda flag: flag != 1 or focus["present"]))
def scan_moves_focus():
    focus["present"] = False
    return {**current, "draft": value}
xlib = ModuleType("Xlib")
xlib.X = SimpleNamespace(AnyPropertyType=0, ShiftMask=1, ControlMask=2, Mod1Mask=4, Mod4Mask=8, Mod5Mask=16,
                        Button1Mask=32, Button2Mask=64, Button3Mask=128)
xroot = SimpleNamespace(get_full_property=lambda *args: SimpleNamespace(value=[100]), query_pointer=lambda: SimpleNamespace(mask=0))
xconnection = SimpleNamespace(screen=lambda: SimpleNamespace(root=xroot), intern_atom=lambda name: name, close=Mock())
xlib.display = SimpleNamespace(Display=lambda *args: xconnection)
with ExitStack() as stack:
    for name, replacement in {
        "visible_menus": Mock(return_value=[]), "status": Mock(side_effect=scan_moves_focus), "window_id": Mock(return_value=100),
        "app": Mock(return_value=application), "document": Mock(return_value=(page, {})), "page_context": Mock(return_value=context),
        "composer": Mock(return_value=entry), "initialize": Mock(return_value=SimpleNamespace(STATE_FOCUSED=1, STATE_ENABLED=2, STATE_EDITABLE=3)),
        "showing": Mock(return_value=True), "text": Mock(return_value=value), "xcommand": Mock(side_effect=AssertionError("Unexpected native input")),
    }.items(): stack.enter_context(patch.object(d, name, replacement))
    stack.enter_context(patch.dict(sys.modules, {"Xlib": xlib}))
    rejected(lambda: d.running_enter_ready(initial, value))
    # A menu opened during that same status scan also blocks Return, even if
    # the underlying Prompt retains its accessibility focused state.
    focus["present"] = True
    menu = {"open": False}
    def scan_opens_menu():
        menu["open"] = True
        return {**current, "draft": value}
    with patch.object(d, "status", side_effect=scan_opens_menu), \
         patch.object(d, "visible_menus", side_effect=lambda: ["menu"] if menu["open"] else []):
        rejected(lambda: d.running_enter_ready(initial, value))


with tempfile.TemporaryDirectory(prefix="bridge-running-send-") as directory:
    root = Path(directory)
    (root / "desktop.lock").touch()
    with patch.object(d, "ROOT", root), patch.object(r, "ROOT", root):
        rid = "c" * 32
        request = {"request_id": rid, "context": context, "text": value, "enter": True}
        ledger = root / "desktop-deliveries" / (rid + ".json")
        sent = {"done": False}
        def button_action(_index):
            record = json.loads(ledger.read_text())
            assert record["state"] == "submitting" and record["running_submission"]["schema"] == 1
            assert record["running_submission"]["expected_text_sha256"] == expected_hash
            assert value not in json.dumps(record, ensure_ascii=False)
            sent["done"] = True
            return True
        action = Mock(side_effect=button_action)
        send_button = SimpleNamespace(name="Send message", getState=lambda: SimpleNamespace(contains=lambda flag: True),
                                      queryAction=lambda: SimpleNamespace(doAction=action))
        stop_action = Mock(side_effect=AssertionError("Stop was clicked"))
        stop = SimpleNamespace(name="Stop response", queryAction=lambda: SimpleNamespace(doAction=stop_action))
        def state(): return {**current, "draft": "" if sent["done"] else value}
        def snapshot(_page, read_text=False):
            return {"feed_id": baseline["feed_id"], "message_id": new.path, "text": value} if read_text else baseline
        common = {
            "check_ready": Mock(return_value=initial), "attachment_paths": Mock(return_value=[]),
            "load_native_attachment_draft": Mock(return_value={**current, "draft": value}),
            "initialize": Mock(return_value=SimpleNamespace(STATE_ENABLED=1, STATE_SENSITIVE=2)),
            "document": Mock(return_value=(page, {})), "composer": Mock(return_value=object()),
            "code_dock": Mock(return_value=object()), "buttons": Mock(return_value=[stop, send_button]),
            "showing": Mock(return_value=True), "status": Mock(side_effect=state),
            "app": Mock(return_value=application), "page_context": Mock(return_value=context),
            "code_panel": Mock(return_value=page), "running_user_snapshot": Mock(side_effect=snapshot),
            "pump_events": Mock(), "xcommand": Mock(),
        }
        with ExitStack() as stack:
            for name, replacement in common.items(): stack.enter_context(patch.object(d, name, replacement))
            stack.enter_context(patch.object(d.time, "sleep"))
            ready, key = common["check_ready"], common["xcommand"]
            receipt = d.send(request)
            assert receipt["state"] == "submitted" and receipt["delivery_mode"] == "running_insert"
            assert d.send(request)["repeated"] is True
            assert action.call_count == 1 and not stop_action.called and not key.called
            ready.assert_called_once_with({**request, "for_send": True})
        # A running attachment request is refused before files or draft loading.
        with patch.object(d, "check_ready", return_value=initial), patch.object(d, "attachment_paths") as files:
            error = rejected(lambda: d.send({**request, "request_id": "d" * 32, "attachment_batch": "e" * 32}))
            assert error.not_submitted is True and not files.called
        # Baseline/focus failures before the atomic submitting marker are known
        # unsubmitted, while the already pasted Ubuntu draft remains intact.
        for error_id, fallback in [("1" * 32, False), ("2" * 32, True)]:
            sent["done"] = False
            with ExitStack() as stack:
                overrides = {**common, "buttons": Mock(return_value=[stop] if fallback else [send_button]),
                             "running_user_snapshot": Mock(return_value=baseline) if fallback else Mock(side_effect=d.DesktopError("Missing baseline")),
                             "running_enter_ready": Mock(side_effect=d.DesktopError("Lost focused Prompt")), "xcommand": Mock()}
                for name, replacement in overrides.items(): stack.enter_context(patch.object(d, name, replacement))
                stack.enter_context(patch.object(d.time, "sleep"))
                prior_actions = action.call_count
                error = rejected(lambda: d.send({**request, "request_id": error_id}))
                assert error.not_submitted is True and action.call_count == prior_actions
                assert not overrides["xcommand"].called
                assert json.loads((root/"desktop-deliveries"/(error_id+".json")).read_text())["state"] == "pasted"
        sent["done"] = False
        failed_action = Mock(side_effect=d.DesktopError("Native submission acknowledgement missing"))
        failed_button = SimpleNamespace(name="Send message", getState=send_button.getState,
                                        queryAction=lambda: SimpleNamespace(doAction=failed_action))
        with ExitStack() as stack:
            overrides = {**common, "buttons": Mock(return_value=[failed_button]), "running_user_snapshot": Mock(return_value=baseline)}
            for name, replacement in overrides.items(): stack.enter_context(patch.object(d, name, replacement))
            stack.enter_context(patch.object(d.time, "sleep"))
            error = rejected(lambda: d.send({**request, "request_id": "3" * 32}))
            assert not getattr(error, "not_submitted", False) and failed_action.call_count == 1
            assert json.loads((root/"desktop-deliveries"/("3"*32+".json")).read_text())["state"] == "submitting"
        # No explicit Send control: only the verified ordinary Return fallback,
        # never Stop/Ctrl+Enter, and only once even when the request is repeated.
        sent["done"] = False
        fallback_request = {**request, "request_id": "f" * 32}
        fallback_ledger = root / "desktop-deliveries" / ("f" * 32 + ".json")
        def ordinary_key(*args):
            assert args == ("key", "Return")
            assert json.loads(fallback_ledger.read_text())["state"] == "submitting"
            sent["done"] = True
        with ExitStack() as stack:
            overrides = {**common, "check_ready": Mock(return_value=initial), "buttons": Mock(return_value=[stop]),
                         "running_enter_ready": Mock(), "xcommand": Mock(side_effect=ordinary_key)}
            for name, replacement in overrides.items(): stack.enter_context(patch.object(d, name, replacement))
            stack.enter_context(patch.object(d.time, "sleep"))
            focused, key = overrides["running_enter_ready"], overrides["xcommand"]
            assert d.send(fallback_request)["state"] == "submitted"
            assert d.send(fallback_request)["repeated"] is True
            key.assert_called_once_with("key", "Return")
            focused.assert_called_once_with(initial, value)
            assert not stop_action.called
        # A delayed queue item may be acknowledged later by the SAME receipt.
        pending = json.loads(ledger.read_text())
        pending["state"] = "submitting"; pending.pop("submission_evidence", None)
        r.atomic(ledger, pending)
        lookup = {"request_id": rid, "fingerprint": pending["fingerprint"]}
        with patch.object(d, "status", return_value=current) as status, \
             patch.object(d, "running_submission_confirmed", return_value=False) as verify, \
             patch.object(d, "send", side_effect=AssertionError("Receipt replayed send")):
            assert not d.delivery_status(lookup)["completed"]
            with patch.object(d.fcntl, "flock", side_effect=BlockingIOError):
                status.reset_mock(); verify.reset_mock()
                assert not d.delivery_status(lookup)["completed"]
                assert not status.called and not verify.called
            with patch.object(d, "running_submission_confirmed", return_value=proof):
                assert d.delivery_status(lookup)["completed"]
                assert json.loads(ledger.read_text())["state"] == "submitted"
        # Legacy and invalid witness receipts do not observe UI or update state.
        for change in ({"running_submission": None}, {"running_submission": {"schema": 99}},
                       {"running_submission": {**pending["running_submission"], "schema": True}}, {"state": "pasted"}):
            r.atomic(ledger, {**pending, **change})
            with patch.object(d, "status", side_effect=AssertionError("Legacy receipt touched UI")):
                assert not d.delivery_status(lookup)["completed"]
        r.atomic(ledger, pending)
        with patch.object(d, "status", side_effect=AssertionError("Mismatched receipt touched UI")):
            assert not d.delivery_status({**lookup, "fingerprint": "0" * 64})["found"]
        # Unknown requests cannot execute another submit action under the same ID.
        with patch.object(d, "check_ready", side_effect=AssertionError("Unknown send replayed")):
            rejected(lambda: d.send(request))

print("Running send checks passed: send-only readiness, strict new-session guard, old-body bound, new exact human evidence, once-only native Send/Return, busy attachments refusal, and lock-bound read-only delayed reconciliation")
