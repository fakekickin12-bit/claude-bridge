"""Native role-query speed paths retain complete document and uniqueness guards."""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d


class Node:
    def __init__(self, role, name="", attrs=(), children=(), visible=True):
        self.role, self.name, self.attrs = role, name, list(attrs)
        self.children, self.visible = list(children), visible

    def __iter__(self): return iter(self.children)
    def getRoleName(self): return self.role
    def getAttributes(self): return self.attrs
    def queryDocument(self): return self
    @property
    def childCount(self): return len(self.children)
    def getChildAtIndex(self, index): return self.children[index]


class Collection:
    def __init__(self, nodes):
        self.nodes, self.requests = nodes, []

    def createMatchRule(self, *arguments):
        self.requests.append(arguments)
        return object()

    def getMatches(self, rule, sort, count, traverse):
        assert count == 0 and traverse is True
        return list(self.nodes)


class NativeRoot(Node):
    def __init__(self, nodes):
        super().__init__("application")
        self.collection = Collection(nodes)

    def queryCollection(self): return self.collection
    @property
    def childCount(self): raise AssertionError("Native role lookup walked the full UI tree")


atspi = SimpleNamespace(**{"ROLE_" + name: index for index, name in enumerate(
    ("DOCUMENT_WEB", "ENTRY", "PUSH_BUTTON", "DIALOG", "ALERT", "MENU", "FILE_CHOOSER"), 1)})
native_api = SimpleNamespace(StateSet=SimpleNamespace(new=lambda values: object()),
                             CollectionMatchType=SimpleNamespace(ALL=1, ANY=2),
                             CollectionSortOrder=SimpleNamespace(CANONICAL=1))
repository = SimpleNamespace(Atspi=native_api)
code = Node("document web", attrs=["URI:https://claude.ai/epitaxy/local_one"])
hidden = Node("document web", attrs=["URI:https://claude.ai/chat/hidden"], visible=False)
foreign = Node("document web", attrs=["URI:https://foreign.example.test/"])
with patch.object(d, "initialize", return_value=atspi), patch.object(d, "pump_events"), \
     patch.object(d, "showing", side_effect=lambda node: node.visible), \
     patch.dict(sys.modules, {"gi": SimpleNamespace(repository=repository), "gi.repository": repository}):
    root = NativeRoot([code, hidden, foreign])
    assert d.document(root)[0] is code
    assert len(root.collection.requests) == 1
    nested_claude = Node("document web", attrs=["URI:https://claude.ai/chat/nested"])
    root.collection.nodes.append(nested_claude)
    with patch.object(d.time, "monotonic", side_effect=[0, 5]):
        try:
            d.document(root)
        except d.DesktopError as error:
            assert "多个" in str(error)
        else:
            raise AssertionError("A second descendant document was ignored")
    root.collection.nodes = [Node("entry", "Prompt"), Node("entry", "Prompt")]
    try:
        d.find("entry", "Prompt", root)
    except d.DesktopError:
        pass
    else:
        raise AssertionError("Duplicate input controls were accepted")

    # Unsupported clients and synthetic nodes retain the original full traversal.
    fallback = Node("application", children=[Node("panel", children=[code]), hidden, foreign])
    assert d.document(fallback)[0] is code

    class Message(Node):
        @property
        def childCount(self): raise AssertionError("UI-chrome lookup entered a message body")

    panel = Node("section", attrs=["class:epitaxy-chat-panel"], children=[Message("article")])
    panel.children.append(Message("panel", "Chat messages", ["xml-roles:feed"]))
    page = Node("document web", children=[panel])
    assert d.code_panel(page) is panel
    page.children.append(Node("section", attrs=["class:epitaxy-chat-panel"]))
    try:
        d.code_panel(page)
    except d.DesktopError:
        pass
    else:
        raise AssertionError("Multiple visible Code panels were accepted")

print("Native collection guards: complete descendant documents, hidden/foreign filters, duplicate inputs, and fallback traversal OK")
