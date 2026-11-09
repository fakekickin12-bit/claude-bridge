"""Generating predicate retains exact behavior without reading ordinary body names."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d


class Node:
    def __init__(self, role, name="", attrs=(), children=(), forbidden=False):
        self.role, self._name, self.attrs = role, name, list(attrs)
        self.children, self.forbidden, self.reads = list(children), forbidden, 0
    @property
    def name(self):
        self.reads += 1
        if self.forbidden: raise AssertionError("An ordinary message body name was read")
        return self._name
    def getRoleName(self): return self.role
    def getAttributes(self): return self.attrs
    @property
    def childCount(self): return len(self.children)
    def getChildAtIndex(self, index): return self.children[index]


canary = Node("static", "Older private reply", forbidden=True)
assert not d.reply_in_progress(Node("article", children=[canary])) and canary.reads == 0
for stop in d.STOP_BUTTONS:
    assert d.reply_in_progress(Node("section", children=[Node("push button", stop)]))
for role in ("section", "static", "article", "panel", "entry", "push button"):
    announcement = Node(role, "Claude is responding", ["container-live:polite"])
    assert d.reply_in_progress(Node("article", children=[announcement]))
assert not d.reply_in_progress(Node("section", children=[Node("static", "Stop response"),
                                                        Node("static", "Claude is responding")]))
assert not d.reply_in_progress(Node("static", "Something else", ["container-live:polite"]))
print("Generating predicate checks passed: body-name canary, every Stop label, announcements of any role inside articles, and non-live/text false positives")
