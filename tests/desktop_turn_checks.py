"""Bounded current-turn capture and durable cursor checks using synthetic native nodes."""
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d


class Node:
    serial = 0

    def __init__(self, role="section", name="", attrs=(), children=(), forbidden=False):
        Node.serial += 1
        self.path = "/org/a11y/atspi/accessible/synthetic_" + str(Node.serial)
        self.role, self._name, self.attrs, self.children = role, name, list(attrs), list(children)
        self.forbidden, self.names, self.visits = forbidden, 0, 0
        self.forbid_name = False

    def touch(self):
        self.visits += 1
        if self.forbidden:
            raise AssertionError("Older history was traversed")

    def getRoleName(self):
        self.touch()
        return self.role

    def getAttributes(self):
        self.touch()
        return self.attrs

    @property
    def name(self):
        self.touch()
        self.names += 1
        if self.forbid_name:
            raise AssertionError("An immutable cached body was read again")
        return self._name

    @property
    def childCount(self):
        self.touch()
        return len(self.children)

    def getChildAtIndex(self, index):
        self.touch()
        return self.children[index]


def paragraph(value):
    return Node("paragraph", attrs=["tag:p"], children=[Node("static", value)])


def row(value):
    return Node(attrs=["class:group/message-row"], children=[paragraph(value)])


def human(label="You said: identical task"):
    body = Node("static", "Current user body must never be read")
    body.forbid_name = True
    return Node("article", children=[Node("heading", label, ["class:sr-only"]),
                                      Node(attrs=["class:group/bubble"], children=[body])])


def page_with(children):
    history = Node(children=children)
    feed = Node("panel", "Chat messages", ["xml-roles:feed"], [Node("paragraph", attrs=["class:sr-only"]), history])
    return Node("document web", children=[feed, Node("push button", "Send")]), history


def leaf(message):
    return message.children[0].children[0]


context = "a" * 64
with tempfile.TemporaryDirectory(prefix="bridge-turn-check-") as directory, \
     patch.object(d, "ROOT", Path(directory)), patch.object(d, "confirm_page_context") as confirm:
    older = Node(forbidden=True)
    prompt = human()
    one, two, three = row("First progress."), row("Second progress."), row("Third progress.")
    status_body = Node("static", "Private tool output")
    status_body.forbid_name = True
    tool = Node(attrs=["class:group/message-row"], children=[Node(attrs=["class:group/status"], children=[status_body])])
    footer = Node(attrs=["class:group/message-row"], children=[Node("tool bar", children=[Node("push button", "Copy")])])
    page, history = page_with([older, prompt, one, tool, two, three, footer])
    first = d.current_turn_reply(page, context)
    assert first["scope"] == "current_turn" and first["message_count"] == first["reply_count"] == 3
    assert first["text"] == "First progress.\n\nSecond progress.\n\nThird progress."
    assert first["read_status"] == "ready" and not first["partial"] and len(first["turn_id"]) == 64
    assert older.visits == status_body.names == prompt.children[1].children[0].names == 0
    cache = Path(directory) / "reply-cursors" / (context + ".json")
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert "Current user body" not in cache.read_text() and "identical task" not in cache.read_text()

    # New helper processes reuse this file: no process-global state is necessary.
    leaf(one).forbid_name = leaf(two).forbid_name = True
    counts = (leaf(one).names, leaf(two).names, leaf(three).names)
    second = d.current_turn_reply(page, context)
    assert second["text"] == first["text"] and second["turn_id"] == first["turn_id"]
    assert (leaf(one).names, leaf(two).names, leaf(three).names) == (counts[0], counts[1], counts[2] + 1)

    # Flush the previous mutable tail once before freezing it when a new row appears.
    leaf(three)._name = "Third progress finalized."
    four = row("Fourth progress.")
    history.children.insert(-1, four)
    extended = d.current_turn_reply(page, context)
    assert extended["message_count"] == 4 and "Third progress finalized." in extended["text"]
    leaf(three).forbid_name = True
    third_reads = leaf(three).names
    assert d.current_turn_reply(page, context)["text"] == extended["text"]
    assert leaf(three).names == third_reads

    # Reorder/virtualization cannot silently discard an already observed middle message.
    unchanged_cache = cache.read_bytes()
    history.children.remove(two)
    missing = d.current_turn_reply(page, context)
    assert missing["read_status"] == "unavailable" and "未跳过中间内容" in missing["read_reason"]
    assert cache.read_bytes() == unchanged_cache
    history.children.insert(4, two)
    assert d.current_turn_reply(page, context)["text"] == extended["text"]

    # An identical repeated prompt is nevertheless a new turn because native identity changed.
    next_prompt, next_reply = human(), row("New turn only.")
    history.children.extend([next_prompt, next_reply])
    reset = d.current_turn_reply(page, context)
    assert reset["text"] == "New turn only." and reset["message_count"] == 1
    assert reset["turn_id"] != first["turn_id"]
    assert "First progress." not in cache.read_text()
    assert next_prompt.children[1].children[0].names == 0

    # Context identity separates caches even if the visible graph is the same.
    other = d.current_turn_reply(page, "b" * 64)
    assert other["turn_id"] != reset["turn_id"] and other["text"] == reset["text"]

    # The same current-turn bootstrap also supports classic Chat assistant articles and code.
    chat = Node("article", children=[Node("heading", "Claude responded", ["class:sr-only"]),
                                    paragraph("Chat reply"), Node(attrs=["tag:pre"], children=[Node("static", "echo ok\nnext")])])
    chat_page, _ = page_with([Node(forbidden=True), human(), chat])
    chat_result = d.current_turn_reply(chat_page, "c" * 64)
    assert chat_result["text"] == "Chat reply\n\necho ok\nnext" and chat_result["code_blocks"] == ["echo ok\nnext"]

    # Unknown article metadata is not skipped across a possible user boundary.
    unknown_page, _ = page_with([human(), Node("article", children=[paragraph("Unclassified")]), row("Newest")])
    assert d.current_turn_reply(unknown_page, "d" * 64)["read_status"] == "unavailable"
    no_anchor = row("No confirmed native human boundary")
    unanchored, _ = page_with([no_anchor])
    assert d.current_turn_reply(unanchored, "e" * 64)["read_status"] == "unavailable"
    assert leaf(no_anchor).names == 0

    # A new row appended during extraction yields a marked ordered prefix, then fills next poll.
    initial_row, appended = row("Before tool"), row("After tool")
    changing_page, changing_history = page_with([human(), initial_row])
    original_reader = d.latest_reply_content
    def append_during_read(node):
        body = original_reader(node)
        if appended not in changing_history.children:
            changing_history.children.append(appended)
        return body
    with patch.object(d, "latest_reply_content", side_effect=append_during_read):
        prefix = d.current_turn_reply(changing_page, "f" * 64)
    assert prefix["partial"] and prefix["text"] == "Before tool" and leaf(appended).names == 0
    filled = d.current_turn_reply(changing_page, "f" * 64)
    assert not filled["partial"] and filled["text"] == "Before tool\n\nAfter tool"

    # A cold turn larger than one poll's time budget checkpoints a contiguous prefix.
    # Each later helper process resumes at that prefix's tail, never starting again at row zero.
    budget_rows = [row("Budget row " + str(index)) for index in range(4)]
    budget_page, _ = page_with([human(), *budget_rows])
    clock = [0]
    def slow_reader(node):
        result = original_reader(node)
        clock[0] += 4
        return result
    with patch.object(d, "TURN_SECONDS", 5), patch.object(d.time, "monotonic", side_effect=lambda: clock[0]), \
         patch.object(d, "latest_reply_content", side_effect=slow_reader):
        budget_first = d.current_turn_reply(budget_page, "4" * 64)
        assert budget_first["partial"] and budget_first["message_count"] == 2
        leaf(budget_rows[0]).forbid_name = True
        budget_second = d.current_turn_reply(budget_page, "4" * 64)
        assert budget_second["partial"] and budget_second["message_count"] == 3
        leaf(budget_rows[1]).forbid_name = True
        budget_final = d.current_turn_reply(budget_page, "4" * 64)
    assert not budget_final["partial"] and budget_final["message_count"] == 4
    assert budget_final["text"] == "\n\n".join("Budget row " + str(index) for index in range(4))

    # Overlapping desktop/mobile captures briefly wait for the same owned cursor lock.
    with patch.object(d.fcntl, "flock", side_effect=[BlockingIOError(), None]) as flock, \
         patch.object(d.time, "sleep") as sleep:
        waited = d.current_turn_reply(changing_page, "f" * 64)
    assert waited["read_status"] == "ready" and flock.call_count == 2 and sleep.call_count == 1
    with patch.object(d.fcntl, "flock", side_effect=BlockingIOError()), patch.object(d, "TURN_LOCK_SECONDS", 0):
        assert d.current_turn_reply(changing_page, "f" * 64)["read_status"] == "unavailable"

    # Budgets, context changes and unsafe caches fail visibly without returning a false complete result.
    limited, _ = page_with([human(), row("Bounded")])
    with patch.object(d, "TURN_MESSAGE_LIMIT", 0):
        assert d.current_turn_reply(limited, "1" * 64)["read_status"] == "unavailable"
    with patch.object(d, "confirm_page_context", side_effect=d.DesktopError("Context changed")):
        assert d.current_turn_reply(limited, "2" * 64)["read_status"] == "unavailable"
    assert not (Path(directory) / "reply-cursors" / ("2" * 64 + ".json")).exists()
    outside = Path(directory) / "private-canary.txt"
    outside.write_text("Must not be read or replaced")
    unsafe = Path(directory) / "reply-cursors" / ("3" * 64 + ".json")
    unsafe.symlink_to(outside)
    assert d.current_turn_reply(limited, "3" * 64)["read_status"] == "unavailable"
    assert outside.read_text() == "Must not be read or replaced"

print("Current turn checks passed: cumulative 1/2/3, durable prefix cache, mutable-tail flush, tools/user/history exclusion, stable turn IDs, missing-middle refusal, partial append, time-budget checkpoint/resume, bounded lock wait and cache guards")
