"""Read-only question cards, bounded history reads and fail-soft waiting notices."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d


class Node:
    def __init__(self, role="section", name="", attrs=(), children=(), selected=False, visible=True, broken=False):
        self.role, self._name, self.attrs, self.children = role, name, list(attrs), list(children)
        self.selected, self.visible, self.broken, self.names = selected, visible, broken, 0

    def getRoleName(self):
        return self.role

    def getAttributes(self):
        return self.attrs

    @property
    def name(self):
        self.names += 1
        if self.broken:
            raise RuntimeError("Native card leaf disappeared")
        return self._name

    @property
    def childCount(self):
        return len(self.children)

    def getChildAtIndex(self, index):
        return self.children[index]

    def getState(self):
        return SimpleNamespace(contains=lambda value: self.selected and value == "pressed")


def text(value, classes=""):
    return Node(attrs=["class:" + classes] if classes else [], children=[Node("static", value)])


def option(label, description="", selected=False):
    return Node("toggle button", label + " " + description, ["class:epitaxy-answer-option"],
                [text(label, "text-body text-primary"), text(description, "text-body text-secondary")], selected)


def question(prompt="Choose a narrator", choices=None):
    return Node("panel", prompt, ["class:epitaxy-approval-card epitaxy-question-card"], [
        text(prompt), Node("push button", "Show previous question"), Node("static", "1 of 3"),
        Node("push button", "Show next question"),
        Node(children=choices if choices is not None else [
            option("Man, 45-55 (Recommended)", "Warm storyteller. Please close the editor first.", True),
            option("Woman, 40-55", "Warm female storyteller."),
            option("No narrator voice", "The video model reads narration."),
            Node(attrs=["class:epitaxy-answer-option"], children=[Node("toggle button", "Other")]),
        ]),
        Node("status bar", "Needs your input", ["class:sr-only"]),
        Node(children=[Node("push button", "Skip"), Node("push button", "Next")]),
    ])


def page(card, history=()):
    footer = Node(attrs=["class:group/message-row"], children=[Node(attrs=["class:group/status"], children=[Node("static", "Needs your input")])])
    feed = Node("panel", "Chat messages", ["xml-roles:feed"], [Node(children=[*history, card, footer])])
    return Node("document web", children=[feed])


atspi = SimpleNamespace(STATE_PRESSED="pressed", STATE_CHECKED="checked", STATE_SELECTED="selected")
with patch.object(d, "showing", side_effect=lambda node: node.visible), patch.object(d, "initialize", return_value=atspi):
    old = Node("article", "A previous private task", broken=True)
    card = question()
    value = d.pending_question(page(card, [old]))
    assert value["read_status"] == "ready", value
    assert value["prompt"] == "Choose a narrator" and value["page"] == 1 and value["total_pages"] == 3
    assert not value["multiple"] and value["actions"] == ["Show previous question", "Show next question", "Skip", "Next"]
    assert len(value["options"]) == 4
    assert value["options"][0] == {"label": "Man, 45-55 (Recommended)",
                                    "description": "Warm storyteller. Please close the editor first.", "selected": True}
    assert value["options"][-1] == {"label": "Other", "description": "", "selected": False}
    rendered = d.question_reply_text(value)
    assert "第 1/3 题" in rendered and "[已选中]" in rendered and "Warm female storyteller" in rendered
    assert "请到 Ubuntu 查看并选择。" in rendered
    assert old.names == 0, "Question discovery must never read an older task body"

    hidden = question(); hidden.visible = False
    assert d.pending_question(page(hidden)) is None
    permission = Node("panel", "Run dangerous command?", ["class:epitaxy-approval-card"], [Node("push button", "Allow")])
    assert d.pending_question(page(permission)) is None, "Permission approvals are not selection questions"
    # Recognized card remains a waiting notice even if a child becomes unreadable.
    broken = question(); broken.children[4].children[0].children[0].children[0].broken = True
    partial = d.pending_question(page(broken))
    assert partial is not None and partial["read_status"] == "unavailable" and not partial["options"]
    assert d.question_reply_text(partial) == d.QUESTION_WAIT_REASON
    ambiguous = d.pending_question(Node(children=[question(), question()]))
    assert ambiguous is not None and ambiguous["read_status"] == "unavailable"

    # Ordinary no-card pages retain their former capture behavior. A current
    # card must survive missing current-turn anchors without leaking old text.
    uri = "https://claude.ai/epitaxy/local_11111111-1111-1111-1111-111111111111"
    context = d.conversation_context(42, uri)
    application = SimpleNamespace(get_process_id=lambda: 42)
    panel = page(card)
    with patch.object(d, "app", return_value=application), \
         patch.object(d, "document", return_value=(panel, {"URI": uri, "Title": "Question check"})), \
         patch.object(d, "code_panel", return_value=panel), \
         patch.object(d, "current_turn_reply", side_effect=AssertionError("Waiting cards must not scan turn/history")), \
         patch.object(d, "page_context", return_value=context), patch.object(d, "confirm_page_context"), \
         patch.object(d, "reply_in_progress", return_value=True):
        captured = d.dispatch({"action": "capture", "context": context})
        assert captured["awaiting_input"] is True and captured["generating"] is False
        assert captured["question"] == value and captured["read_status"] == "ready"
        assert captured["scope"] == "latest" and captured["message_count"] == 1
        assert "Choose a narrator" in captured["text"] and "Unverified old text" not in captured["text"]
        assert captured["read_reason"] == d.QUESTION_WAIT_REASON
        with patch.object(d, "pending_question", return_value=partial):
            unreadable = d.dispatch({"action": "capture", "context": context})
            assert unreadable["awaiting_input"] and unreadable["text"] == d.QUESTION_WAIT_REASON

print("Question capture checks passed: native toggle options/descriptions/selection, visible page numbers, no history bodies, permission exclusion, unreadable-card wait notice and missing-anchor fail-soft capture")
