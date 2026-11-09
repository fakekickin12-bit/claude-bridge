"""Two-level Code navigation: synthetic AX only, never real UI or model calls."""
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
    def __init__(self, flags): self.flags = flags
    def contains(self, flag): return flag in self.flags


class Node:
    def __init__(self, role="section", name="", classes="", children=(), attrs=None, flags=None):
        self.role, self._name, self.classes = role, name, classes
        self.children, self.attrs = list(children), dict(attrs or {})
        self.flags = set(flags if flags is not None else {"show", "enabled", "sensitive"})
        self.callback, self.parent, self.clicks, self.name_reads = None, None, 0, 0
        self.forbid_name = False
        for child in self.children: child.parent = self

    @property
    def name(self):
        if self.forbid_name: raise AssertionError("An unselected session title was read")
        self.name_reads += 1
        return self._name
    def __iter__(self): return iter(self.children)
    @property
    def childCount(self): return len(self.children)
    def getChildAtIndex(self, index): return self.children[index]
    def getRoleName(self): return self.role
    def getAttributes(self):
        return ["class:" + self.classes] + [key + ":" + value for key, value in self.attrs.items()]
    def getState(self): return State(self.flags)
    def get_process_id(self): return 42
    def queryAction(self): return self
    def doAction(self, index):
        assert index == 0
        self.clicks += 1
        if self.callback: self.callback()
        return True


atspi = SimpleNamespace(STATE_SHOWING="show", STATE_ENABLED="enabled", STATE_SENSITIVE="sensitive",
                        STATE_EXPANDABLE="expandable", STATE_EXPANDED="expanded", STATE_SELECTED="selected")


class Fixture:
    def __init__(self):
        self.first = Node("push button", "First session", "hit-row-main", attrs={"current": "page"})
        self.dup_a = Node("push button", "Duplicate", "hit-row-main")
        self.dup_b = Node("push button", "Duplicate", "hit-row-main")
        self.alpha_header = Node("push button", "Alpha", "group/label",
                                 flags={"show", "enabled", "sensitive", "expandable", "expanded"})
        self.alpha = Node(classes="group/section", children=[self.alpha_header, self.first, self.dup_a, self.dup_b])
        self.beta_header = Node("push button", "Beta", "group/label",
                                flags={"show", "enabled", "sensitive", "expandable"})
        self.target = Node("push button", "Recent", "hit-row-main", flags={"enabled", "sensitive"})
        self.older = Node("push button", "Older", "hit-row-main", flags={"enabled", "sensitive"})
        self.beta = Node(classes="group/section", children=[self.beta_header, self.target, self.older])
        self.sidebar = Node(classes="dframe-sidebar", children=[self.alpha, self.beta])
        self.page = Node("document web", children=[self.sidebar])
        self.application = Node("application", children=[self.page])
        self.attrs = {"URI": "https://claude.ai/epitaxy/local_11111111-1111-1111-1111-111111111111"}
        self.conditions = {"draft": "", "attachments": [], "dialogs": [], "busy": True,
                           "approval": True, "window_visible": True, "locked": False}
        self.guard_calls = 0
        self.beta_header.callback = self.expand
        self.target.callback = lambda: self.select(self.target, "2")
        self.older.callback = lambda: self.select(self.older, "3")

    def expand(self):
        self.beta_header.flags.add("expanded")
        self.target.flags.add("show")
        self.older.flags.add("show")

    def select(self, node, digit):
        for item in (self.first, self.target, self.older): item.attrs.pop("current", None)
        node.attrs["current"] = "page"
        self.attrs["URI"] = "https://claude.ai/epitaxy/local_" + digit * 8 + "-" + "-".join([digit * 4] * 3) + "-" + digit * 12

    def status(self):
        self.guard_calls += 1
        return {"context": d.conversation_context(42, self.attrs["URI"]), "mode": "Code",
                "ready": False, "session_id": self.attrs["URI"].split("claude.ai")[1], **self.conditions}

    def forbid(self, nodes, value=True):
        for node in nodes: node.forbid_name = value

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        for module, name, options in (
            (d, "ROOT", {"new": Path(self.temp.name)}), (r, "ROOT", {"new": Path(self.temp.name)}),
            (d, "app", {"return_value": self.application}),
            (d, "document", {"side_effect": lambda *args: (self.page, dict(self.attrs))}),
            (d, "initialize", {"return_value": atspi}), (d, "status", {"side_effect": self.status}),
            (d, "desktop_locked", {"side_effect": lambda: self.conditions["locked"]}),
            (d, "visible_menus", {"return_value": []}), (d.time, "sleep", {})):
            self.stack.enter_context(patch.object(module, name, **options))
        return self

    def __exit__(self, *args):
        self.stack.close()
        self.temp.cleanup()


def rejected(call):
    try: call()
    except d.DesktopError as exc: return exc
    raise AssertionError("Unsafe project selection was accepted")


def packet(catalog, project_id, session="", action="switch_project"):
    return {"action": action, "context": catalog["context"], "catalog_id": catalog["catalog_id"],
            "project_id": project_id, "session_id": session,
            "session_catalog_id": catalog.get("session_catalog_id", "")}


with Fixture() as f:
    all_sessions = [f.first, f.dup_a, f.dup_b, f.target, f.older]
    f.forbid(all_sessions)
    headers = d.dispatch({"action": "list_projects"})
    alpha, beta = headers["projects"]
    assert headers["scope"] == "projects" and headers["selected_project_id"] == ""
    assert all(not project["sessions"] and project["sessions_loaded"] is False for project in headers["projects"])
    assert not any(node.name_reads for node in all_sessions)
    assert f.guard_calls == 0 and all(node.clicks == 0 for node in all_sessions)
    signature = headers["catalog_id"]
    f.target._name = "Renamed unseen recent"
    assert d.list_projects()["catalog_id"] == signature
    f.forbid([f.first, f.dup_a, f.dup_b], False)
    scoped = d.dispatch({"action": "project_catalog", "project_id": alpha["id"],
                         "context": headers["context"], "catalog_id": signature})
    assert scoped["scope"] == "sessions" and scoped["selected_project_id"] == alpha["id"]
    assert len(scoped["projects"][0]["sessions"]) == 3
    assert sum(not item["selectable"] for item in scoped["projects"][0]["sessions"]) == 2
    assert not scoped["projects"][1]["sessions_loaded"] and not scoped["projects"][1]["sessions"]
    assert f.target.name_reads == f.older.name_reads == 0
    f.forbid([f.first, f.dup_a, f.dup_b])
    f.forbid([f.target, f.older], False)
    expanded = d.dispatch(packet(headers, beta["id"]))
    assert expanded["expanded"] and not expanded["switched"] and "target" not in expanded
    assert f.beta_header.clicks == 1 and f.target.clicks == f.older.clicks == 0
    assert f.guard_calls == 1
    session_id = expanded["projects"][1]["sessions"][0]["id"]
    for key, value in [("draft", "Draft"), ("attachments", ["file.txt"]), ("dialogs", ["Dialog"]),
                       ("locked", True), ("window_visible", False)]:
        old = f.conditions[key]
        f.conditions[key] = value
        rejected(lambda: d.switch_project(packet(expanded, beta["id"], session_id)))
        assert f.target.clicks == 0
        f.conditions[key] = old
    for key in ("context", "catalog_id", "session_catalog_id"):
        bad = packet(expanded, beta["id"], session_id)
        bad[key] = "f" * 64
        rejected(lambda: d.switch_project(bad))
    bad = packet(expanded, beta["id"], session_id)
    bad.pop("session_catalog_id")
    rejected(lambda: d.switch_project(bad))
    previous = expanded["session_catalog_id"]
    f.older._name = "Renamed older"
    rejected(lambda: d.switch_project(packet(expanded, beta["id"], session_id)))
    expanded = d.dispatch({"action": "project_catalog", "project_id": beta["id"]})
    assert expanded["session_catalog_id"] != previous
    switched = r.dispatch({"transport": "desktop", **packet(expanded, beta["id"], session_id)})
    assert switched["switched"] and f.target.clicks == 1 and f.older.clicks == 0
    assert switched["target"]["context"] == switched["context"] != expanded["context"]
    assert switched["target"]["ready"] is False and "draft" not in switched["target"]
    before = f.guard_calls
    same = d.switch_project(packet(switched, beta["id"], session_id))
    assert not same["switched"] and f.target.clicks == 1 and f.guard_calls == before
    assert d.list_projects()["scope"] == "projects"

with Fixture() as f:
    f.forbid([f.first, f.dup_a, f.dup_b])
    headers = d.list_projects()
    beta_id = headers["projects"][1]["id"]
    opened = d.dispatch(packet(headers, beta_id, action="open_project"))
    assert opened["switched"] and opened["expanded"] and f.target.clicks == 1
    assert f.older.clicks == 0 and f.guard_calls == 2
    before = f.guard_calls
    f.select(f.older, "3")
    reopened = d.open_project(packet(d.list_projects(), beta_id))
    assert not reopened["switched"] and reopened["active_session_id"] == d.project_selection_id("session", beta_id, "Older")
    assert f.guard_calls == before and f.older.clicks == 0

for failure in ("empty", "duplicate_first", "disabled_first", "blank_first", "duplicate_active"):
    with Fixture() as f:
        f.expand()
        if failure == "empty": f.beta.children = [f.beta_header]
        elif failure == "duplicate_first": f.older._name = f.target._name
        elif failure == "disabled_first": f.target.flags.remove("enabled")
        elif failure == "blank_first": f.target._name = ""
        elif failure == "duplicate_active": f.target.attrs["current"] = f.older.attrs["current"] = "page"
        headers = d.list_projects()
        error = rejected(lambda: d.open_project(packet(headers, headers["projects"][1]["id"])))
        assert error.not_submitted is True
        if failure == "empty": assert "该项目暂无历史对话" in str(error)
        assert f.target.clicks == f.older.clicks == 0

with Fixture() as f:
    headers = d.list_projects()
    f.beta_header._name = "Alpha"
    changed = d.list_projects()
    assert all(not item["selectable"] for item in changed["projects"])
    rejected(lambda: d.open_project(packet(headers, headers["projects"][1]["id"])))
    assert f.beta_header.clicks == f.target.clicks == 0

with Fixture() as f:
    f.expand()
    scoped = d.read_project_catalog({"project_id": d.list_projects()["projects"][1]["id"]})
    beta_id = scoped["selected_project_id"]
    session_id = scoped["projects"][1]["sessions"][0]["id"]
    def stale_guard():
        result = f.status()
        f.older._name = "Changed during readiness check"
        return result
    with patch.object(d, "status", side_effect=stale_guard):
        error = rejected(lambda: d.switch_project(packet(scoped, beta_id, session_id)))
    assert error.not_submitted is True and f.target.clicks == 0

with Fixture() as f:
    f.expand()
    scoped = d.read_project_catalog({"project_id": d.list_projects()["projects"][1]["id"]})
    beta_id = scoped["selected_project_id"]
    session_id = scoped["projects"][1]["sessions"][0]["id"]
    f.target.callback = lambda: None  # Native action accepted, destination never confirmed.
    with patch.object(d.time, "monotonic", side_effect=[0, 1, 11]):
        error = rejected(lambda: d.switch_project(packet(scoped, beta_id, session_id)))
    assert not getattr(error, "not_submitted", False) and f.target.clicks == 1

for target_action in ("open_project", "switch_project"):
    with Fixture() as f:
        if target_action == "switch_project": f.expand()
        initial = d.list_projects()
        beta_id = initial["projects"][1]["id"]
        if target_action == "switch_project":
            initial = d.read_project_catalog({"project_id": beta_id})
            session_id = initial["projects"][1]["sessions"][0]["id"]
        else:
            session_id = ""
        original_headers = d.project_headers
        calls = [0]
        def edited_during_catalog():
            result = original_headers()
            calls[0] += 1
            if calls[0] == 2:
                f.conditions["draft"] = "Draft typed while the directory was being checked"
            return result
        with patch.object(d, "project_headers", side_effect=edited_during_catalog):
            error = rejected(lambda: d.dispatch(packet(initial, beta_id, session_id, target_action)))
        assert error.not_submitted is True and f.target.clicks == f.beta_header.clicks == 0

print("Two-level project checks passed: names-only/scoped privacy, signatures, strict recent/active choice, manual compatibility, guards, duplicate/empty rows and routing")
