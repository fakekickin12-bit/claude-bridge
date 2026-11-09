"""Reply reads are bound to the same page and PID before and after extraction."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_remote as d


uri = "https://claude.ai/epitaxy/local_11111111-1111-1111-1111-111111111111"
page, panel = object(), object()
application = SimpleNamespace(get_process_id=lambda: 42)
context = d.conversation_context(42, uri)
attrs = {"URI": uri, "Title": "Synthetic title"}


def rejected(call):
    try: call()
    except d.DesktopError: return
    raise AssertionError("A reply from a changed page was exposed")


with patch.object(d, "app", return_value=application), patch.object(d, "document", return_value=(page, attrs)), \
     patch.object(d, "code_panel", return_value=panel), patch.object(d, "reply_in_progress", return_value=False), \
     patch.object(d, "current_turn_reply", return_value={"text": "Selected reply", "reply_count": 1, "code_blocks": [], "scope": "current_turn"}) as body:
    result = d.dispatch({"action": "capture", "context": context})
    assert result["context"] == context and result["route"] == attrs["URI"].split("claude.ai")[1]
    assert result["text"] == "Selected reply" and result["mode"] == "Code"
    body.assert_called_once_with(panel, context)
    body.reset_mock()
    rejected(lambda: d.dispatch({"action": "capture", "context": "f" * 64}))
    body.assert_not_called()
    other = {"URI": "https://claude.ai/epitaxy/local_22222222-2222-2222-2222-222222222222"}
    with patch.object(d, "document", side_effect=[(page, attrs), (page, other)]):
        rejected(lambda: d.dispatch({"action": "capture", "context": context}))
    body.assert_called_once()
    body.reset_mock()
    restarted = SimpleNamespace(get_process_id=lambda: 43)
    with patch.object(d, "app", side_effect=[application, restarted]):
        rejected(lambda: d.dispatch({"action": "capture", "context": context}))
    body.assert_called_once()
    body.reset_mock()
    # Legacy read-only callers may follow the current page, but receive its exact identity.
    assert d.dispatch({"action": "capture"})["context"] == context

print("Capture context checks passed: exact page/PID binding, stale-body refusal, restart refusal and legacy identity return")
