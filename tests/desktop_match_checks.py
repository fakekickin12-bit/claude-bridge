"""Read-only current-task matching: synthetic AX, never a native click or model call."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d
import remote as r


class State:
    def __init__(self, selected=False): self.selected = selected
    def contains(self, flag): return self.selected and flag == "selected"


class Node:
    def __init__(self, name="", classes="", current="", selected=False, children=(), visible=True):
        self._name, self.classes, self.current, self.selected = name, classes, current, selected
        self.children, self.visible, self.parent = list(children), visible, None
        self.forbid_name, self.name_reads = False, 0
        for child in self.children: child.parent = self

    @property
    def name(self):
        assert not self.forbid_name, "An unrelated task title or reply body was read"
        self.name_reads += 1
        return self._name
    def getAttributes(self): return ["class:" + self.classes, "current:" + self.current]
    def getState(self): return State(self.selected)
    def get_process_id(self): return 42
    def queryAction(self): raise AssertionError("Matching must never query a native action")


def descendants(node):
    for child in node.children:
        yield child
        yield from descendants(child)


class Fixture:
    def __init__(self):
        self.header = Node("sample-project", "group/label")
        self.task = Node("Synthetic current task", "hit-row-main", current="page")
        self.other = Node("Unrelated private task", "hit-row-main")
        self.other.forbid_name = True
        self.group = Node(classes="group/section", children=[self.header, self.task, self.other])
        self.other_header = Node("Private other project", "group/label")
        self.other_header.forbid_name = True
        self.other_task = Node("Private other task", "hit-row-main")
        self.other_task.forbid_name = True
        self.other_group = Node(classes="group/section", children=[self.other_header, self.other_task])
        self.sidebar = Node(classes="dframe-sidebar", children=[self.group, self.other_group])
        self.body = Node("Unrelated reply body", "article")
        self.body.forbid_name = True
        self.page = Node(children=[self.sidebar, self.body])
        self.application = Node(children=[self.page])
        self.attrs = {"URI": "https://claude.ai/epitaxy/local_11111111-1111-1111-1111-111111111111",
                      "Title": "Claude Code"}
        self.query_roots = []

    def buttons(self, root):
        self.query_roots.append(root)
        return [node for node in descendants(root) if node.classes in {"group/label", "hit-row-main"}]

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(r, "ROOT", Path(self.temp.name)))
        for name, options in (
                ("app", {"return_value": self.application}),
                ("document", {"side_effect": lambda *args: (self.page, dict(self.attrs))}),
                ("initialize", {"return_value": SimpleNamespace(STATE_SELECTED="selected")}),
                ("showing", {"side_effect": lambda node: node.visible}),
                ("buttons", {"side_effect": self.buttons})):
            self.stack.enter_context(patch.object(d, name, **options))
        # Reading is valid with a busy task, pending approval, and existing drafts.
        # None of their mutable/readiness-dependent paths may run at all.
        for name in ("status", "check_ready", "project_headers", "project_catalog", "activate", "focus_composer",
                     "send", "start", "desktop_locked", "walk", "latest_reply"):
            self.stack.enter_context(patch.object(d, name, side_effect=AssertionError("Unexpected operation: " + name)))
        return self

    def __exit__(self, *args):
        self.stack.close()
        self.temp.cleanup()


def rejected(call, phrase=""):
    try: call()
    except d.DesktopError as error:
        assert phrase in str(error)
        return
    raise AssertionError("Ambiguous or changed current task was accepted")


with Fixture() as f:
    result = r.dispatch({"transport": "desktop", "action": "match_current", "context": "stale old context"})
    expected = d.conversation_context(42, f.attrs["URI"])
    assert result["matched"] and result["context"] == expected
    assert result["project_title"] == "sample-project"
    assert result["session_title"] == f.task._name == result["target"]["title"]
    assert result["target"]["session_id"] == result["target"]["native_session_id"] == f.attrs["URI"].split("claude.ai")[1]
    assert result["target"]["context"] == expected and result["target"]["ready"] is False
    assert result["target"]["new_session"] is False and "draft" not in result["target"]
    assert f.query_roots == [f.page, f.group]
    assert not any(node.name_reads for node in (f.other, f.other_header, f.other_task, f.body))
    assert not (Path(f.temp.name) / "desktop.lock").exists()

with Fixture() as f:
    f.task.current, f.task.selected = "", True
    assert d.match_current({})["session_title"] == f.task._name

with Fixture() as f:
    f.other.current = "page"
    rejected(lambda: d.match_current({}), "不唯一")
    assert f.other.name_reads == 0

with Fixture() as f:
    f.task.current = ""
    f.attrs["Title"] = "A task with hidden sidebar – Claude Code"
    result = d.match_current({})
    assert result["session_title"] == "A task with hidden sidebar" and result["project_title"] == ""
    assert f.task.name_reads == f.header.name_reads == 0 and f.query_roots == [f.page]
    f.attrs["Title"] = "Claude Code"
    rejected(lambda: d.match_current({}), "展开")

with Fixture() as f:
    f.task.visible = False
    f.attrs["Title"] = "Hidden task - Claude Code"
    assert d.match_current({})["session_title"] == "Hidden task"

with Fixture() as f:
    f.attrs["URI"] = "https://claude.ai/epitaxy"
    rejected(lambda: d.match_current({}), "已有本机")
    assert not f.query_roots
    f.attrs["URI"] = "https://claude.ai/chat/other"
    rejected(lambda: d.match_current({}), "已有本机")

with Fixture() as f:
    other = {"URI": "https://claude.ai/epitaxy/local_22222222-2222-2222-2222-222222222222"}
    with patch.object(d, "document", side_effect=[(f.page, dict(f.attrs)), (f.page, other)]):
        rejected(lambda: d.match_current({}), "变化")
    with patch.object(d, "app", side_effect=[f.application, SimpleNamespace(get_process_id=lambda: 43)]):
        rejected(lambda: d.match_current({}), "变化")

with Fixture() as f:
    original_buttons = f.buttons
    def changed_marker(root):
        result = original_buttons(root)
        if root is f.group:
            f.task.current = ""
        return result
    with patch.object(d, "buttons", side_effect=changed_marker):
        rejected(lambda: d.match_current({}), "正在变化")

print("Current task match checks passed: active metadata only, read-only busy/draft tolerance, hidden-sidebar fallback, ambiguity/context/PID guards and desktop routing")
