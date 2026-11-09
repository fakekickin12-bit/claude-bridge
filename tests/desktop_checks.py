"""Ubuntu-only transport guard checks; synthetic files, no client input or submission."""
import hashlib
import io
import json
import sys
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path.home() / ".local/share/claude-bridge"))
import desktop_remote as d
import remote as r

class Paragraph:
    def __init__(self, raw): self.raw = raw
    def getRoleName(self): return "paragraph"
    def queryText(self): return self
    def getText(self, *args): return self.raw

class Composer:
    def queryText(self): return self
    def getText(self, *args): return "\ufffc" * 4
    def __iter__(self): return iter(map(Paragraph, ["first", "\n", "第三行", "last"]))

assert d.text(Composer()) == "first\n\n第三行\nlast"

class Node:
    def __init__(self, role, name="", attrs=(), children=(), visible=True):
        self.role, self.name, self.attrs, self.children = role, name, list(attrs), list(children)
        self.visible = visible
        self.parent = None
        for child in self.children:
            child.parent = self
    def getRoleName(self): return self.role
    def getAttributes(self): return self.attrs
    def queryDocument(self): return self
    def __iter__(self): return iter(self.children)
    @property
    def childCount(self): return len(self.children)
    def getChildAtIndex(self, index): return self.children[index]
    def get_process_id(self): return 42

def paragraph(value): return Node("paragraph", attrs=["tag:p"], children=[Node("static", value)])
old = Node("article", children=[Node("heading", "Claude responded: earlier", ["class:sr-only"]), paragraph("Earlier reply")])
latest = Node("article", children=[Node("section", attrs=["class:group/message-row"], children=[
    Node("heading", "Claude responded: Hello world", ["class:sr-only"]),
    Node("section", attrs=["class:group/status"], children=[Node("static", "Used several tools")]),
    Node("paragraph", attrs=["tag:p"], children=[Node("static", "Hello "), Node("static", attrs=["tag:strong"], children=[Node("static", "world")])]),
    Node("section", attrs=["tag:pre"], children=[Node("static", 'print("ok")\n# second line')]),
    Node("tool bar", "Message actions", children=[Node("static", "Copy"), Node("static", "9 minutes ago", ["tag:time"])]),
])])
user = Node("article", children=[Node("heading", "You said: private prompt", ["class:sr-only"]), paragraph("Private user prompt")])
feed = Node("panel", "Chat messages", ["xml-roles:feed"], [user, old, latest])
page = Node("document web", children=[Node("static", "Navigation"), feed, Node("static", "Claude is AI and can make mistakes. Opus 5.5")])
clean = d.latest_reply(page)
assert clean["reply_count"] == 2 and clean["text"].startswith("Hello world")
assert 'print("ok")\n# second line' in clean["text"]
assert clean["code_blocks"] == ['print("ok")\n# second line']
assert not d.reply_in_progress(page)
assert d.reply_in_progress(Node("document web", children=[Node("push button", "Stop response")]))
assert all(x not in clean["text"] for x in ["Earlier reply", "Private user prompt", "Navigation", "Opus 5.5", "9 minutes ago", "Used several tools", "Copy"])
assert d.latest_reply(Node("document web"))["text"] == ""

code_uri = "https://claude.ai/epitaxy/local_12345678-1234-1234-1234-123456789abc"
code_prompt = Node("entry", "Prompt")
inline_approval = Node("section", children=[Node("push button", "Not now")], visible=False)
model_controls = Node("section", attrs=["class:z-0"], children=[Node("push button", "Model: Opus")])
dock = Node("section", attrs=["class:group/approval-dock"], children=[
    Node("section", children=[code_prompt, Node("push button", "Send")]), model_controls, inline_approval,
])
code_reply = Node("article", children=[
    Node("heading", "Claude responded", ["class:sr-only"]), paragraph("Latest Code reply"),
    Node("section", attrs=["tag:pre"], children=[Node("static", "git status\ngit diff")]),
])
code_feed = Node("section", attrs=["xml-roles:feed"], children=[user, old, code_reply])
code_panel = Node("section", attrs=["class:epitaxy-chat-panel"], children=[code_feed, dock])
secondary_panel = Node("section", attrs=["class:epitaxy-chat-panel"], children=[Node("entry", "Prompt")], visible=False)
code_document = Node("document web", attrs=[f"URI:{code_uri}", "Title:Claude Code"], children=[
    Node("entry", "Prompt"), Node("entry", "Write your prompt to Claude"), feed, secondary_panel, code_panel,
])
hidden_chat = Node("document web", attrs=["URI:https://claude.ai/chat/example"], visible=False)
another_claude_page = Node("document web", attrs=["URI:https://claude.ai/chat/another"])
unrelated_page = Node("document web", attrs=["URI:https://claude.ai.example/epitaxy"])
application = Node("application", children=[hidden_chat, unrelated_page])

with patch.object(d, "showing", side_effect=lambda node: node.visible):
    with patch.object(d, "pump_events") as pump, patch.object(d.time, "sleep", side_effect=lambda _: application.children.append(code_document)):
        selected_page, code_attrs = d.document(application)
        assert selected_page is code_document and d.code_page(code_attrs)
        assert pump.call_count == 2
    assert d.composer(code_document, code_attrs) is code_prompt
    assert d.code_dock(code_prompt) is dock
    assert not d.code_approval_pending(dock, code_prompt)
    inline_approval.visible = True
    assert d.code_approval_pending(dock, code_prompt)
    with patch.object(d, "document", return_value=(code_document, code_attrs)), patch.object(d, "app", return_value=application):
        captured = d.dispatch({"action": "capture"})
    assert captured["mode"] == "Code"
    assert captured["reply_count"] == 2
    assert captured["text"].startswith("Latest Code reply")
    assert captured["code_blocks"] == ["git status\ngit diff"]
    assert all(value not in captured["text"] for value in ["Private user prompt", "Earlier reply", "Not now", "Hello world"])
    secondary_panel.visible = True
    try:
        d.composer(code_document, code_attrs)
        raise AssertionError("multiple visible Code panels accepted")
    except d.DesktopError:
        pass
    for pages, message in [([unrelated_page], "暂时读不到"), ([code_document, another_claude_page], "多个 Claude 页面")]:
        with patch.object(d, "pump_events"), patch.object(d.time, "monotonic", side_effect=[0, 5]):
            try:
                d.document(Node("application", children=pages))
                raise AssertionError("missing or ambiguous Claude document accepted")
            except d.DesktopError as error:
                assert message in str(error)

with tempfile.TemporaryDirectory(prefix="bridge-new-code-check-") as directory:
    workspace = Path(directory) / "project-two"
    workspace.mkdir()
    other_workspace = Path(directory) / "other-project"
    other_workspace.mkdir()
    new_code_attrs = {"URI": "https://claude.ai/epitaxy", "Title": "New Code task"}
    environment_selector = Node("push button", "Environment: This computer", ["id:environment-selector"])
    folder_selector = Node("push button", "Folder: " + str(workspace), ["id:folder-selector"])
    new_prompt = Node("entry", "Prompt")
    footer = Node("section", attrs=["class:w-full"], children=[
        environment_selector, folder_selector, Node("push button", "Model: Opus"),
        Node("push button", "Permissions: Bypass permissions"), Node("push button", "Usage: 7%"),
    ])
    new_dock = Node("section", attrs=["class:group/approval-dock"], children=[
        Node("section", children=[new_prompt, Node("push button", "Send")]), footer,
    ])
    with patch.object(d, "showing", side_effect=lambda node: node.visible):
        target = d.new_code_target(new_code_attrs, new_dock)
        assert target["workspace"] == str(workspace.resolve())
        assert target["selector_ids"] == ["environment-selector", "folder-selector"]
        assert d.new_code_target({**new_code_attrs, "URI": "https://claude.ai/epitaxy/"}, new_dock) == target
        assert d.new_code_target({"URI": code_uri}, new_dock) is None
        assert not d.code_approval_pending(new_dock, new_prompt)

        folder_selector.name = "Folder: " + str(other_workspace)
        switched_target = d.new_code_target(new_code_attrs, new_dock)
        assert switched_target["workspace"] == str(other_workspace.resolve())
        assert switched_target != target
        for invalid_path in ["relative-project", str(workspace / "missing")]:
            folder_selector.name = "Folder: " + invalid_path
            assert d.new_code_target(new_code_attrs, new_dock) is None
        folder_selector.name = "Folder: " + str(workspace)
        folder_selector.visible = False
        assert d.new_code_target(new_code_attrs, new_dock) is None
        folder_selector.visible = True
        environment_selector.name = "Environment: Cloud"
        assert d.new_code_target(new_code_attrs, new_dock) is None
        environment_selector.name = "Environment: This computer"
        footer.children.append(Node("push button", "Environment: This computer", ["id:duplicate-environment"]))
        assert d.new_code_target(new_code_attrs, new_dock) is None
        footer.children.pop()

        for label in ["Approve", "Sign in again"]:
            approval = Node("section", children=[Node("push button", label)])
            new_dock.children.append(approval)
            assert d.code_approval_pending(new_dock, new_prompt)
            new_dock.children.pop()
            footer.children.append(Node("push button", label))
            assert d.code_approval_pending(new_dock, new_prompt)
            footer.children.pop()
        footer.children.append(Node("radio button", "Allow this action"))
        assert d.code_approval_pending(new_dock, new_prompt)
        footer.children.pop()
        assert not d.code_approval_pending(new_dock, new_prompt)

existing_initial = {"context": "existing-context", "new_session": False}
existing_receipt = {"context": "existing-context", "draft": "", "attachments": []}
assert d.code_submission_confirmed(existing_initial, existing_receipt, "Existing task")
assert not d.code_submission_confirmed(existing_initial, {**existing_receipt, "draft": "Pending"}, "Existing task")
try:
    d.code_submission_confirmed(existing_initial, {**existing_receipt, "context": "another-session"}, "Existing task")
    raise AssertionError("existing-session switch accepted after submission")
except d.DesktopError:
    pass

new_initial = {"new_session": True, "process_id": 42, "context": "new-task-context"}
new_receipt = {"mode": "Code", "process_id": 42, "new_session": False,
    "session_id": "/epitaxy/local_12345678-1234-1234-1234-123456789abc", "draft": "", "attachments": [],
    "context": hashlib.sha256(("42|" + code_uri).encode()).hexdigest()}
pending_new_task = {**new_receipt, "new_session": True, "setup_visible": True, "context": "new-task-context"}
assert not d.code_submission_confirmed(new_initial, pending_new_task, "First task")
starting_new_task = {**pending_new_task, "setup_visible": False, "session_id": None,
    "context": "new-task-footer-unmounted", "busy": True}
with patch.object(d, "document") as document:
    assert not d.code_submission_confirmed(new_initial, starting_new_task, "First task")
    assert not document.called
for changes in [
        {"setup_visible": True}, {"draft": "Another draft"}, {"attachments": ["pending.txt"]}]:
    try:
        d.code_submission_confirmed(new_initial, {**starting_new_task, **changes}, "First task")
        raise AssertionError("changed setup or draft accepted as first-send transition")
    except d.DesktopError:
        pass
for changes in [
        {"mode": "Chat"}, {"process_id": 43}, {"session_id": None},
        {"new_session": True, "context": "changed-project-context"}]:
    try:
        d.code_submission_confirmed(new_initial, {**new_receipt, **changes}, "First task")
        raise AssertionError("unrelated new-task receipt accepted")
    except d.DesktopError:
        pass
with patch.object(d, "document") as document:
    assert not d.code_submission_confirmed(new_initial, {**new_receipt, "draft": "Pending"}, "First task")
    assert not d.code_submission_confirmed(new_initial, {**new_receipt, "attachments": ["file.txt"]}, "First task")
    assert not document.called

submitted_prompt = Node("article", children=[
    Node("heading", "You said", ["class:sr-only"]), paragraph("First\n  task"),
])
submitted_panel = Node("section", attrs=["class:epitaxy-chat-panel"], children=[submitted_prompt])
submitted_document = Node("document web", children=[submitted_panel])
with patch.object(d, "showing", side_effect=lambda node: node.visible), \
     patch.object(d, "document", return_value=(submitted_document, {"URI": code_uri})):
    assert d.code_submission_confirmed(new_initial, new_receipt, "First task")
    for prompt_nodes in [
            [Node("article", children=[Node("heading", "You said", ["class:sr-only"]), paragraph("Different task")])],
            [Node("article", children=[Node("heading", "You said", ["class:sr-only"]), paragraph("Prior task")]), submitted_prompt]]:
        submitted_panel.children = prompt_nodes
        try:
            d.code_submission_confirmed(new_initial, new_receipt, "First task")
            raise AssertionError("mismatched first message or old conversation accepted")
        except d.DesktopError:
            pass
    submitted_panel.children = []
    assert not d.code_submission_confirmed(new_initial, new_receipt, "First task")
    submitted_panel.children = [submitted_prompt]
    with patch.object(d, "document", return_value=(submitted_document, {"URI": "https://claude.ai/epitaxy/local_different"})):
        try:
            d.code_submission_confirmed(new_initial, new_receipt, "First task")
            raise AssertionError("document switched while reading first-send receipt")
        except d.DesktopError:
            pass
    for marker in ["Message collapsed", "Message expanded"]:
        user_bubble = Node("section", attrs=["class:bg-[var(--cds-bg-user-message)]"], children=[
            paragraph("First task"), Node("static", marker),
        ])
        submitted_panel.children = [Node("article", children=[
            Node("heading", "You said", ["class:sr-only"]), user_bubble,
        ])]
        assert d.code_submission_confirmed(new_initial, new_receipt, "First task")
        user_bubble.children.insert(1, paragraph(marker))
        assert d.code_submission_confirmed(new_initial, new_receipt, "First task\n" + marker)
        try:
            d.code_submission_confirmed(new_initial, new_receipt, "First task")
            raise AssertionError("literal collapse-state phrase removed from user paragraph")
        except d.DesktopError:
            pass
    submitted_panel.children = [submitted_prompt]

with tempfile.TemporaryDirectory(prefix="bridge-first-send-check-") as directory:
    root = Path(directory)
    initial = {**new_initial, "mode": "Code", "workspace": str(root)}
    empty_composer = {**pending_new_task, "session_id": None, "busy": False, "dialogs": [], "approval": False}
    pasted_composer = {**empty_composer, "draft": "First task"}
    different_session = {**new_receipt, "context": "another-local-context", "session_id": "/epitaxy/local_another"}
    for later_states, verdicts, should_submit in [
            ([d.DesktopError("Page transitioning"), new_receipt], [True], True),
            ([starting_new_task, new_receipt], [False, True], True),
            ([new_receipt, different_session], [False], False)]:
        send_button = Mock()
        send_button.name = "Send"
        send_button.getState.return_value.contains.return_value = True
        send_button.queryAction.return_value.doAction.return_value = True
        request_id = uuid.uuid4().hex
        packet = {"text": "First task", "request_id": request_id, "context": initial["context"], "enter": True}
        with ExitStack() as mocks:
            mocks.enter_context(patch.object(r, "ROOT", root))
            mocks.enter_context(patch.object(d, "ROOT", root))
            mocks.enter_context(patch.object(d, "check_ready", return_value=initial))
            mocks.enter_context(patch.object(d, "attachment_paths", return_value=[]))
            mocks.enter_context(patch.object(d, "status", side_effect=[empty_composer, pasted_composer, pasted_composer, *later_states]))
            paste = mocks.enter_context(patch.object(d, "paste"))
            mocks.enter_context(patch.object(d, "initialize"))
            mocks.enter_context(patch.object(d, "document", return_value=(submitted_document, {"URI": code_uri})))
            mocks.enter_context(patch.object(d, "composer", return_value=code_prompt))
            mocks.enter_context(patch.object(d, "code_dock", return_value=dock))
            mocks.enter_context(patch.object(d, "buttons", return_value=[send_button]))
            mocks.enter_context(patch.object(d, "showing", return_value=True))
            mocks.enter_context(patch.object(d, "pump_events"))
            mocks.enter_context(patch.object(d.time, "sleep"))
            verifier = mocks.enter_context(patch.object(d, "code_submission_confirmed", side_effect=verdicts))
            if should_submit:
                sent = d.send(packet)
                assert sent["state"] == "submitted"
                assert sent["initial_context"] == initial["context"]
                assert sent["context"] == new_receipt["context"]
                assert sent["session_id"] == new_receipt["session_id"]
                assert sent["workspace"] == str(root)
                assert d.send(packet)["repeated"] is True
            else:
                try:
                    d.send(packet)
                    raise AssertionError("second Code session transition accepted")
                except d.DesktopError:
                    pass
                saved = json.loads((root / "desktop-deliveries" / (request_id + ".json")).read_text())
                assert saved["state"] == "submitting"
                assert verifier.call_count == 1
            assert paste.call_count == 1
            send_button.queryAction.return_value.doAction.assert_called_once_with(0)

with patch.object(d, "initialize"), patch.object(d, "desktop_locked", return_value=False), \
     patch.object(d, "activate", side_effect=[d.WindowUnavailable("hidden"), 123]), \
     patch.object(d, "launch_client") as launch, patch.object(d.time, "sleep"):
    assert d.start({"desktop_bin": "/existing/wrapper"})["restored"] is True
    assert launch.call_count == 1
with patch.object(d, "initialize"), patch.object(d, "desktop_locked", return_value=True), patch.object(d, "launch_client") as launch:
    try:
        d.start({})
        raise AssertionError("locked desktop accepted")
    except d.DesktopError:
        assert not launch.called
hidden = {"context": "same", "ready": False, "input_ready": True}
visible = {"context": "same", "title": "existing chat", "ready": True}
with patch.object(d, "status", side_effect=[hidden, visible]), \
     patch.object(d, "window_id", side_effect=d.WindowUnavailable("hidden")), patch.object(d, "start") as restore:
    assert d.check_ready({"context": "same"})["context"] == "same"
    assert restore.call_count == 1
with patch.object(d, "status", side_effect=[hidden, {**visible, "context": "changed"}]), \
     patch.object(d, "window_id", side_effect=d.WindowUnavailable("hidden")), patch.object(d, "start"):
    try:
        d.check_ready({"context": "same"})
        raise AssertionError("changed conversation after restore accepted")
    except d.DesktopError:
        pass
with patch.object(d, "status", return_value={"context": "current", "ready": True}), patch.object(d, "window_id"):
    try:
        d.check_ready({"context": "different"})
        raise AssertionError("changed conversation accepted")
    except d.DesktopError:
        pass
with patch.object(d, "status", return_value={"context": "current", "ready": False}), patch.object(d, "window_id") as window:
    try:
        d.check_ready({})
        raise AssertionError("busy/draft state accepted")
    except d.DesktopError:
        assert not window.called

with tempfile.TemporaryDirectory(prefix="bridge-desktop-check-") as directory:
    root = Path(directory)
    with patch.object(r, "ROOT", root), patch.object(d, "ROOT", root):
        request = {"session": "bridge-qa", "batch_id": uuid.uuid4().hex}
        workspace = root / "workspace"
        workspace.mkdir()
        r.atomic(root / "sessions/bridge-qa.json", {"cwd": str(workspace)})
        video_id = uuid.uuid4().hex
        entries = []
        for title, role, payload in [("source.mp4", "original", b"preserved-video"), ("video-timeline.jpg", "preview", b"synthetic-preview"), ("metadata.json", "metadata", b"{}")]:
            entries.append({"id": video_id if role == "original" else uuid.uuid4().hex, "name": title, "role": role,
                "kind": "video" if role == "original" else "file", "parent_id": None if role == "original" else video_id,
                "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "payload": payload})
        r.attachment_prepare({**request, "files": [{k:v for k,v in e.items() if k != "payload"} for e in entries]})
        for e in entries:
            r.receive({**request, "file_id": e["id"]}, io.BytesIO(e["payload"]))
        r.attachment_finish(request)
        paths = d.attachment_paths({"session": request["session"], "attachment_batch": request["batch_id"]})
        assert len(paths) == 2 and all(not p.endswith(".mp4") for p in paths)
        assert {Path(p).suffix for p in paths} == {".jpg", ".json"}
        rid = uuid.uuid4().hex
        packet = {"text": "Test", "request_id": rid, "enter": True, "context": "current", "attachment_batch": None}
        fingerprint = hashlib.sha256(json.dumps(["Test", None, "current", True]).encode()).hexdigest()
        ledger = root / "desktop-deliveries" / (rid + ".json")
        r.atomic(ledger, {"state": "submitted", "fingerprint": fingerprint})
        with patch.object(d, "paste") as paste, patch.object(d, "drop_files") as drop, patch.object(d, "check_ready") as ready:
            assert d.send(packet)["repeated"] is True
            assert not paste.called and not drop.called and not ready.called
            r.atomic(ledger, {"state": "submitting", "fingerprint": fingerprint})
            try:
                d.send(packet)
                raise AssertionError("unknown delivery retried")
            except d.DesktopError:
                assert not paste.called and not drop.called
print(json.dumps({"ok": True, "paragraph_readback": True, "draft_busy_guard": True, "context_guard": True,
    "video_uses_actual_previews": True, "idempotency": True, "unknown_outcome_no_retry": True,
    "background_window_restore": True, "locked_desktop_guard": True, "restore_keeps_conversation_guard": True,
    "latest_reply_only": True, "reply_metadata_removed": True, "code_newlines_preserved": True,
    "desktop_code_document_and_composer": True, "code_inline_approval_guard": True, "code_reply_only": True,
    "new_code_workspace_selection": True, "new_code_footer_approval_guard": True, "new_code_submission_receipt": True,
    "new_code_one_transition_only": True, "new_code_created_session_ledger": True, "new_code_setup_unmount_pending": True,
    "new_code_collapse_marker_scoped": True}))
