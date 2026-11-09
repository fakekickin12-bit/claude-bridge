"""Latest native message reads: synthetic AX only, no history bodies or native actions."""
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d


class Node:
    def __init__(self, role="section", name="", attrs=(), children=(), forbid=False):
        self.role, self._name = role, name
        self.attrs, self.children = list(attrs), list(children)
        self.forbid, self.reads, self.child_reads, self.name_reads = forbid, 0, [], 0

    def touch(self):
        self.reads += 1
        if self.forbid:
            raise AssertionError("An older message body was touched")

    def getRoleName(self):
        self.touch()
        return self.role

    def getAttributes(self):
        self.touch()
        return self.attrs

    @property
    def name(self):
        self.touch()
        self.name_reads += 1
        return self._name

    @property
    def childCount(self):
        self.touch()
        return len(self.children)

    def getChildAtIndex(self, index):
        self.touch()
        self.child_reads.append(index)
        return self.children[index]


def paragraph(value):
    return Node("paragraph", attrs=["tag:p"], children=[Node("static", value)])


def assistant(*children):
    return Node(attrs=["class:group/message-row"], children=children)


def wrapper(child):
    return Node(children=[child])


def feed_page(children, outside=()):
    history = Node(children=children)
    feed = Node("panel", "Chat messages", ["xml-roles:feed"], [Node("paragraph", attrs=["class:sr-only"]), history])
    return Node("document web", children=[feed, *outside]), feed, history


# Current Code AX: a human article, plain assistant row wrappers, and an empty footer row.
old_body = Node("static", "Private historical body", forbid=True)
old_row = assistant(Node("paragraph", children=[old_body]))
older = [wrapper(old_row) for _ in range(10000)]
user = Node("article", children=[
    Node("heading", "You said: private new task", ["class:sr-only"]),
    Node(attrs=["class:group/bubble"], children=[Node("static", "User body", forbid=True)]),
])
text = paragraph("Only the final answer.")
code = Node(attrs=["tag:pre"], children=[Node("static", 'print("keep")\n# same code')])
last = assistant(text, code, Node("tool bar", "Message actions", children=[Node("push button", "Copy")]))
footer = Node(children=[Node("status bar"), Node("tool bar", children=[Node("push button", "Copy")]),
                        assistant(Node("section", attrs=["class:sr-only"], children=[Node("static", "Footer canary", forbid=True)]),
                                  Node("status bar"), Node("tool bar", children=[Node("push button", "Copy")]))])
toolbar_only = wrapper(assistant(Node("tool bar", "Message actions", children=[Node("push button", "Copy")])))
page, feed, history = feed_page([*older, wrapper(user), wrapper(last), toolbar_only, footer])
result = d.latest_reply(page)
assert result["read_status"] == "ready" and result["scope"] == "latest" and result["reply_count"] == 1
assert result["text"] == 'Only the final answer.\n\nprint("keep")\n# same code'
assert result["code_blocks"] == ['print("keep")\n# same code']
assert old_row.reads == old_body.reads == user.reads == 0
assert history.child_reads == [10003, 10002, 10001], "Only the footer/action and last reply wrappers may be fetched"
assert feed.child_reads == [1], "The feed's old children are not enumerated up front"


# A newest human message must be rejected before its body (including fake assistant styling).
latest_user = Node("article", children=[
    Node("heading", "You said: next instruction", ["class:sr-only"]),
    assistant(Node("static", "Human body canary", forbid=True)),
])
page, _, history = feed_page([wrapper(last), wrapper(latest_user)])
result = d.latest_reply(page)
assert result["text"].startswith("Only the final answer") and latest_user.children[1].reads == 0


# A genuine pending row with an empty paragraph must not fall back to an old answer.
pending = assistant(paragraph(""))
old_canary = Node("article", forbid=True)
page, _, _ = feed_page([old_canary, wrapper(pending)])
result = d.latest_reply(page)
assert result["read_status"] == "empty" and not result["text"] and result["reply_count"] == 1
assert old_canary.reads == 0 and "暂时没有正文" in result["read_reason"]


# Old Chat UI still exposes assistant articles; shallow metadata selects one only.
chat_last = Node("article", children=[Node("heading", "Claude responded", ["class:sr-only"]), paragraph("Last Chat reply")])
page, _, _ = feed_page([Node("article", forbid=True), chat_last])
assert d.latest_reply(page)["text"] == "Last Chat reply"


# Missing/ambiguous/over-budget structure is an explicit read failure, never a successful blank.
assert d.latest_reply(Node("document web"))["read_status"] == "unavailable"
first, _, _ = feed_page([])
second, _, _ = feed_page([])
assert d.latest_reply(Node("document web", children=[first, second]))["read_status"] == "unavailable"
deep = Node()
for _ in range(40):
    deep = Node(children=[deep])
assert d.latest_reply(deep)["read_status"] == "unavailable"
empty, _, _ = feed_page([])
assert d.latest_reply(empty)["read_reason"] == "当前会话还没有 Claude 回复。"


# Old history announcements or Stop controls never keep translation stuck in generating.
old_announcement = assistant(Node("static", "Claude is responding", ["container-live:polite"]))
page, feed, _ = feed_page([wrapper(old_announcement)], [Node("push button", "Send")])
feed.child_reads.clear()
assert not d.reply_in_progress(page) and not feed.child_reads
page, feed, _ = feed_page([Node("article", forbid=True)], [Node("push button", "Stop response")])
assert d.reply_in_progress(page)
assert not feed.child_reads


# Message extraction respects a content budget without reading an older answer.
page, _, _ = feed_page([Node("article", forbid=True), wrapper(last)])
with patch.object(d, "REPLY_BODY_LIMIT", 1):
    assert d.latest_reply(page)["read_status"] == "unavailable"

print("Latest reply checks passed: 10000-history bound, current Code rows, Chat articles, empty footer, human rejection, one-pass code, pending/limits, and pruned generating history")
