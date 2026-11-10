"""Linux attachment transport regressions. Synthetic files/state only, no GUI input."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d
import remote as r


class Node:
    def __init__(self, role, name="", attrs=(), children=(), action=None):
        self.role, self.name, self.attrs = role, name, list(attrs)
        self.children, self.visible, self.action = list(children), True, action
        self.parent = None
        for child in self.children:
            child.parent = self

    def __iter__(self): return iter(self.children)
    def getRoleName(self): return self.role
    def getAttributes(self): return self.attrs
    @property
    def childCount(self): return len(self.children)
    def getChildAtIndex(self, index): return self.children[index]
    def queryAction(self): return self
    def doAction(self, index): return self.action() if self.action else True


def fails(operation):
    try:
        operation()
    except d.DesktopError:
        return
    raise AssertionError("Unsafe attachment state was accepted")


# A read-only transport preflight may inspect window properties, but may not
# create a window, own a selection, inject input, send XDND, or change focus.
xlib, extension = ModuleType("Xlib"), ModuleType("Xlib.ext")
xlib.X = SimpleNamespace(AnyPropertyType=0, IsViewable=2)
xlib.Xatom = SimpleNamespace(ATOM=4)
window = SimpleNamespace(
    get_wm_class=Mock(return_value=("claude", "Claude")),
    get_attributes=Mock(return_value=SimpleNamespace(map_state=2)),
)
properties = {
    "_NET_WM_PID": SimpleNamespace(value=[42]),
    "XdndAware": SimpleNamespace(value=[5], property_type=4, format=32),
}
window.get_full_property = Mock(side_effect=lambda atom, kind: properties.get(atom))
connection = SimpleNamespace(
    create_resource_object=Mock(return_value=window), intern_atom=lambda name: name,
    has_extension=Mock(return_value=True), close=Mock(),
)
xlib.display = SimpleNamespace(Display=Mock(return_value=connection))
xlib.protocol = SimpleNamespace(event=SimpleNamespace(ClientMessage=Mock(side_effect=AssertionError("Native protocol event was constructed"))))
extension.xtest = SimpleNamespace(fake_input=Mock(side_effect=AssertionError("Native input was injected")))
with patch.dict(sys.modules, {"Xlib": xlib, "Xlib.ext": extension}), \
     patch.object(d, "window_id", return_value=100) as selected_window, \
     patch.object(d, "environment", return_value={"DISPLAY": "synthetic"}):
    assert d.attachment_transport_probe(42) == {"ready": True, "reason": ""}
    connection.create_resource_object.assert_called_with("window", 100)
    connection.close.assert_called_once()
    for field, invalid in [
        ("_NET_WM_PID", None), ("_NET_WM_PID", SimpleNamespace(value=[41])),
        ("_NET_WM_PID", SimpleNamespace(value=[42, 43])), ("XdndAware", None),
        ("XdndAware", SimpleNamespace(value=[2], property_type=4, format=32)),
        ("XdndAware", SimpleNamespace(value=[5], property_type=99, format=32)),
        ("XdndAware", SimpleNamespace(value=[5], property_type=4, format=8)),
        ("XdndAware", SimpleNamespace(value=[5, 5], property_type=4, format=32)),
    ]:
        original = properties[field]; properties[field] = invalid
        assert d.attachment_transport_probe(42)["ready"] is False
        properties[field] = original
    with patch.object(window, "get_attributes", return_value=SimpleNamespace(map_state=0)):
        assert d.attachment_transport_probe(42)["ready"] is False
    with patch.object(window, "get_wm_class", return_value=("unrelated",)):
        assert d.attachment_transport_probe(42)["ready"] is False
    with patch.object(connection, "has_extension", return_value=False):
        assert d.attachment_transport_probe(42)["ready"] is False
    with patch.object(d, "window_id", side_effect=d.DesktopError("Ambiguous Claude windows")):
        result = d.attachment_transport_probe(42)
        assert not result["ready"] and result["reason"] == "Ambiguous Claude windows"
    assert d.attachment_transport_probe(0)["ready"] is False
    xlib.protocol.event.ClientMessage.assert_not_called()
    extension.xtest.fake_input.assert_not_called()
with patch.dict(sys.modules, {"Xlib": None}):
    assert d.attachment_transport_probe(42)["ready"] is False


paths = [str(Path.home() / "bridge-test" / "unique.png"), str(Path.home() / "bridge-test" / "unique.txt")]
labels = ["unique.png", "~/bridge-test/unique.txt"]
assert d.match_native_attachments(labels, paths) == dict(zip(paths, labels))
assert d.match_native_attachments(labels[:1], paths, partial=True) == {paths[0]: labels[0]}
for names, expected in [
        (labels[:1], paths), ([labels[0], labels[0]], paths),
        (["unique.png", "~/different-project/unique.txt"], paths),
        (["unexpected.txt"], paths), (["unique.png"], [paths[0], str(Path.home() / "elsewhere" / "unique.png")])]:
    fails(lambda: d.match_native_attachments(names, expected))

with patch.object(d, "showing", side_effect=lambda n: n.visible):
    own = Node("push button", "Remove " + labels[1])
    tile = Node("section", attrs=["class:group/tile"], children=[own])
    shelf = Node("section", attrs=["class:epitaxy-attachment-shelf"], children=[tile])
    dock = Node("section", children=[Node("push button", "Remove unrelated.txt"), shelf])
    assert d.native_attachment_controls(dock, True) == {labels[1]: own}
    duplicate = Node("push button", "Remove " + labels[1])
    shelf.children.append(duplicate)
    fails(lambda: d.native_attachment_controls(dock, True))
    shelf.children.pop()
    own.visible = False
    assert not d.native_attachment_controls(dock, True)

with tempfile.TemporaryDirectory(prefix="bridge-native-check-") as directory:
    root = Path(directory)
    signature = {"schema": 1, "synthetic": True}
    with patch.object(d, "ROOT", root), patch.object(r, "ROOT", root), \
         patch.object(d, "attachment_support_signature", return_value=signature), \
         patch.object(d, "attachment_transport_probe", return_value={"ready": True, "reason": ""}) as transport:
        # A fresh install has no private verification marker. Eligibility means
        # current native structure plus the current window's XDND transport,
        # never that a particular file is already loaded or approved.
        assert d.native_attachment_capabilities(42, True, None, None, True) == {"native_attachments": True}
        r.atomic(root / "native-attachment-support.json", signature)
        assert d.native_attachment_capabilities(42, True, None, None, True) == {"native_attachments": True}
        assert d.native_attachment_capabilities(42, True, None, None, False)["native_attachments"] is False
        r.atomic(root / "native-attachment-support.json", {"schema": 2})
        marker_before = (root / "native-attachment-support.json").read_bytes()
        assert d.native_attachment_capabilities(42, True, None, None, True) == {"native_attachments": True}
        assert (root / "native-attachment-support.json").read_bytes() == marker_before
        with patch.object(d, "attachment_support_signature", return_value=None):
            transport.reset_mock()
            unsupported = d.native_attachment_capabilities(42, True, None, None, True)
            assert unsupported["native_attachments"] is False and unsupported["attachment_reason"]
            transport.assert_not_called()
        with patch.object(d, "attachment_support_signature", side_effect=RuntimeError("Native shape changed")):
            assert d.native_attachment_capabilities(42, True, None, None, True)["native_attachments"] is False
        with patch.object(d, "attachment_transport_probe", return_value={"ready": False, "reason": "Synthetic transport missing"}):
            unsupported = d.native_attachment_capabilities(42, True, None, None, True)
            assert unsupported == {"native_attachments": False, "attachment_reason": "Synthetic transport missing"}

        uri = "https://claude.ai/epitaxy/local_12345678-1234-1234-1234-123456789abc"
        context = d.conversation_context(42, uri)
        state = {"context": context, "mode": "Code", "title": "Synthetic Code", "process_id": 42,
                 "new_session": False, "draft": "", "attachments": [], "busy": False,
                 "dialogs": [], "approval": False, "attachment_pending": False,
                 "capabilities": {"native_attachments": True}}
        initial = {**state, "ready": True}
        for change in ({"locked": True}, {"window_visible": False}, {"approval": True}, {"busy": True}, {"awaiting_input": True}):
            fails(lambda: d.require_attachment_draft({**state, **change}, context, [], ""))
        assert d.project_target_summary(state)["capabilities"] == {"native_attachments": True}
        rid = "a" * 32
        request = {"request_id": rid, "text": "Synthetic draft only.", "enter": False,
                   "context": context, "attachment_batch": "b" * 32}

        def drop(selected, expected_context):
            assert selected == paths and expected_context == context
            state["attachments"] = list(labels)

        def paste(value, expected_context):
            assert expected_context == context
            state["draft"] = value.strip()

        def current():
            return {**state, "attachments": list(state["attachments"])}

        with patch.object(d, "check_ready", return_value=initial), \
             patch.object(d, "attachment_paths", return_value=paths), \
             patch.object(d, "drop_files", side_effect=drop) as dropped, \
             patch.object(d, "paste", side_effect=paste) as pasted, \
             patch.object(d, "status", side_effect=current), patch.object(d.time, "sleep"), \
             patch.object(d, "find") as find:
            result = d.send(request)
            assert result["state"] == "pasted" and result["native_labels"] == labels
            assert dropped.call_count == pasted.call_count == 1 and not find.called
            assert d.send(request)["repeated"] is True
            assert dropped.call_count == pasted.call_count == 1

        record = json.loads((root / "desktop-deliveries" / (rid + ".json")).read_text())
        assert record["attachment_paths"] == paths and record["auto_enter"] is False
        assert record["draft_sha256"] == hashlib.sha256(state["draft"].encode()).hexdigest()

        controls_removed = []
        entry = Node("entry", "Prompt")

        def controls(_entry):
            tiles = []
            for label in state["attachments"]:
                def remove(label=label):
                    controls_removed.append(label)
                    state["attachments"].remove(label)
                    return True
                tiles.append(Node("section", attrs=["class:group/tile"], children=[
                    Node("push button", "Remove " + label, action=remove)]))
            return Node("section", children=[Node("section", attrs=["class:epitaxy-attachment-shelf"], children=tiles)])

        def key(*arguments):
            assert arguments == ("key", "--clearmodifiers", "ctrl+a", "BackSpace")
            state["draft"] = ""

        with patch.object(d, "status", side_effect=current), \
             patch.object(d, "document", return_value=(Node("document web"), {"URI": uri})), \
             patch.object(d, "composer", return_value=entry), patch.object(d, "code_dock", side_effect=controls), \
             patch.object(d, "text", side_effect=lambda n: state["draft"]), \
             patch.object(d, "showing", side_effect=lambda n: n.visible), \
             patch.object(d, "focus_composer") as focus, patch.object(d, "xcommand", side_effect=key), \
             patch.object(d.time, "sleep"):
            result = d.cleanup_attachment_draft({"request_id": rid, "context": context})
            assert result["state"] == "cleared" and not state["draft"] and not state["attachments"]
            assert controls_removed == labels
            focus.assert_called_once_with(context, expected_draft=request["text"])

        for change in ({"context": "changed"}, {"draft": "User edited"}, {"attachments": labels + ["foreign.txt"]}):
            r.atomic(root / "desktop-deliveries" / (rid + ".json"), record)
            altered = {**state, "draft": request["text"], "attachments": list(labels), **change}
            with patch.object(d, "status", return_value=altered), patch.object(d, "xcommand") as keyboard, \
                 patch.object(d, "document") as document:
                fails(lambda: d.cleanup_attachment_draft({"request_id": rid, "context": context}))
                assert not keyboard.called and not document.called

        with patch.object(d, "check_ready", return_value={**initial, "capabilities": {"native_attachments": False}}), \
             patch.object(d, "drop_files") as drop_unverified:
            fails(lambda: d.send({**request, "request_id": "c" * 32}))
            assert not drop_unverified.called

        def unrelated_drop(selected, expected_context):
            state.update(draft="", attachments=["unrelated.png"])

        with patch.object(d, "check_ready", return_value=initial), \
             patch.object(d, "attachment_paths", return_value=paths), \
             patch.object(d, "drop_files", side_effect=unrelated_drop), patch.object(d, "status", side_effect=current), \
             patch.object(d, "paste") as never_paste:
            fails(lambda: d.send({**request, "request_id": "d" * 32}))
            assert not never_paste.called

print("Native attachment guards: read-only current transport eligibility, exact per-delivery path/chip matching, draft-only receipt, owned cleanup, and stale/user/question changes OK")
