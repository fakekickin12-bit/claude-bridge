"""Desktop viewer contracts, receipts and input guards without touching a real desktop."""
import base64
import contextlib
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import socket
import stat
import struct
import sys
import tempfile
import time
import types
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_stream as d
import desktop_remote as remote_desktop


def refused(function, *args):
    try:
        function(*args)
    except (d.StreamError, OSError):
        return
    raise AssertionError("Expected an explicit refusal")


class FakeImage:
    format, size = "JPEG", (80, 60)
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Native:
    def __init__(self, clock):
        self.clock, self.events, self.closed = clock, [], False
        self.geometry, self.owner = (0, 0, 1920, 1080), "compositor-original"
        self.locked, self.unknown_lock, self.fail, self.before_input = False, False, False, None
        self.not_before = None
    def identity(self): return self.owner, self.geometry
    def require_unlocked(self):
        if self.locked or self.unknown_lock: raise d.StreamError("Locked or unknown")
    def frame(self, not_before=0):
        self.not_before = not_before
        return b"test JPEG bytes", 80, 60, self.clock()
    def perform(self, event):
        if self.before_input: self.before_input()
        self.events.append(dict(event))
        if self.fail: raise RuntimeError("Uncertain native response")
    def close(self): self.closed = True


def input_request(viewer, frame, event, rid=None):
    return {"action": "desktop_input", "viewer_id": viewer, "session_id": frame["session_id"],
            "frame_id": frame["frame_id"], "request_id": rid or __import__("uuid").uuid4().hex, "event": event}


click = {"kind": "click", "x": .5, "y": .75, "button": 1, "clicks": 1}
viewer, other = "a" * 32, "b" * 32
for event in [click, {"kind": "scroll", "x": 1, "y": 0, "delta_x": -10, "delta_y": 10},
              {"kind": "drag", "x": 0, "y": .1, "to_x": 1, "to_y": 1, "button": 1},
              {"kind": "text", "text": "中文\n$() ` not shell"}, *({"kind": "key", "key": key} for key in d.KEYS)]:
    assert d.validate_event(event) == event
for event in [dict(click, x=float("nan")), dict(click, y=float("inf")), dict(click, x=True),
              dict(click, button=True), dict(click, clicks=3), dict(click, x=-.1), dict(click, extra="unknown"),
              {"kind": "key", "key": "$(touch /tmp/never)"}, {"kind": "text", "text": "a\0b"},
              {"kind": "text", "text": "x" * 20001}, {"type": "click"}]:
    refused(d.validate_event, event)

fake_pil = types.ModuleType("PIL")
fake_pil.Image = types.SimpleNamespace(open=lambda raw: FakeImage())
with tempfile.TemporaryDirectory(prefix="desktop-stream-check-") as folder, \
     patch.object(d, "ROOT", Path(folder)), patch.dict(sys.modules, {"PIL": fake_pil}):
    clock = [10000.0]
    instances = []
    def factory():
        instance = Native(lambda: clock[0]); instances.append(instance); return instance
    controller = d.StreamController(factory, clock=lambda: clock[0])
    first = controller.frame(viewer)
    assert first["width"] == 80 and first["desktop_width"] == 1920 and first["mime"] == "image/jpeg"
    assert base64.b64decode(first["image_base64"]) == b"test JPEG bytes"
    assert first["expires_at"] == first["captured_at"] + 45
    assert stat.S_IMODE((Path(folder) / "desktop-inputs").stat().st_mode) == 0o700
    backend = instances[-1]
    second_view = controller.frame(other)
    assert len(instances) == 1
    cross = input_request(other, first, click)
    assert controller.input(cross)["state"] == "not_applied" and not backend.events

    before_click = controller.frame(viewer)
    no_focus = controller.input(input_request(viewer, before_click, {"kind": "text", "text": "Must not type"}))
    assert no_focus["state"] == "not_applied" and not backend.events
    request = input_request(viewer, before_click, click)
    expected = hashlib.sha256(json.dumps([viewer, request["session_id"], request["frame_id"], click], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    backend.before_input = lambda: (
        controller.receipts.read(request["request_id"])["state"] == "pending" or (_ for _ in ()).throw(AssertionError("Input preceded receipt")))
    applied = controller.input(request)
    backend.before_input = None
    assert applied["state"] == "applied" and applied["matched"] and applied["completed"] and applied["fingerprint"] == expected
    assert len(backend.events) == 1 and not controller.grants
    assert controller.input(request) == applied and len(backend.events) == 1
    refused(controller.input, {**request, "event": dict(click, x=.2)})
    assert controller.input(input_request(other, second_view, click))["state"] == "not_applied"

    text_frame = controller.frame(viewer)
    assert backend.not_before == controller.last_input
    text = "中文 private task body $()"
    text_request = input_request(viewer, text_frame, {"kind": "text", "text": text})
    assert controller.input(text_request)["state"] == "applied"
    receipt_path = Path(folder) / "desktop-inputs" / (text_request["request_id"] + ".json")
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600 and text not in receipt_path.read_text()
    assert "event" not in json.loads(receipt_path.read_text())
    refused_text_frame = controller.frame(viewer)
    with patch.object(backend, "perform", side_effect=d.InputNotApplied("Plaintext clipboard unavailable")):
        safe_refusal = controller.input(input_request(viewer, refused_text_frame, {"kind": "text", "text": "Do not overwrite image"}))
    assert safe_refusal["state"] == "not_applied" and safe_refusal["reason"] == "Plaintext clipboard unavailable" and not backend.closed

    # Resize, compositor restart, stale grants, locks and concurrent Bridge sends refuse before input.
    for mutation in ("geometry", "owner", "locked", "unknown_lock", "expired"):
        frame = controller.frame(viewer)
        original_geometry, original_owner = backend.geometry, backend.owner
        if mutation == "geometry": backend.geometry = (0, 0, 2560, 1440)
        elif mutation == "owner": backend.owner = "compositor-restarted"
        elif mutation == "expired": controller.grants[frame["frame_id"]]["expires"] = clock[0] - 1
        else: setattr(backend, mutation, True)
        count = len(backend.events)
        result = controller.input(input_request(viewer, frame, click))
        assert result["state"] == "not_applied" and len(backend.events) == count
        backend.geometry, backend.owner, backend.locked, backend.unknown_lock = original_geometry, original_owner, False, False
    frame = controller.frame(viewer)
    with d.owned_lock(Path(folder) / "desktop.lock"):
        assert controller.input(input_request(viewer, frame, click))["state"] == "not_applied"

    # Once execution starts an uncertain reply stays unknown, including after a new helper process.
    frame = controller.frame(viewer); unknown_request = input_request(viewer, frame, click)
    backend.fail = True
    unknown = controller.input(unknown_request)
    count = len(backend.events)
    assert unknown["state"] == "unknown" and not unknown["completed"] and backend.closed
    assert controller.backend is None
    assert controller.input(unknown_request)["state"] == "unknown" and len(backend.events) == count
    backend.fail = False
    receipts = d.Receipts()
    assert receipts.status(viewer, unknown_request["request_id"], unknown["fingerprint"])["state"] == "unknown"
    assert not receipts.status(other, unknown_request["request_id"], unknown["fingerprint"])["matched"]
    assert not receipts.status(viewer, "f" * 32, "f" * 64)["found"]
    pending_rid = "d" * 32
    pending_fp = d.input_fingerprint(viewer, frame["session_id"], frame["frame_id"], click)
    receipts.write({"request_id": pending_rid, "viewer_id": viewer, "fingerprint": pending_fp, "state": "pending"})
    pending_request = input_request(viewer, frame, click, pending_rid)
    assert controller.input(pending_request)["state"] == "unknown" and len(backend.events) == count
    with patch.object(d, "connect_daemon", return_value=None), patch.object(d.subprocess, "Popen") as popen:
        assert d.dispatch(pending_request)["state"] == "unknown"
        assert not popen.called
        absent_request = input_request(viewer, frame, click)
        assert d.dispatch(absent_request)["state"] == "not_applied"
        assert not popen.called

    # Closing/expiry removes only our own cast. One remaining viewer keeps the cast alive.
    controller.frame(other)
    backend = instances[-1]
    controller.close(viewer); assert not backend.closed
    clock[0] += 31; controller.expire(); assert backend.closed and not controller.viewers
    replacement = controller.frame(viewer)
    assert replacement["session_id"] != first["session_id"] and len(instances) == 3
    controller.shutdown(); assert instances[-1].closed

    # Bounded protocol refuses a large announced packet before reading its body.
    one, two = socket.socketpair()
    try:
        d.transmit(one, {"ok": True}, 100)
        assert d.receive(two, 100) == {"ok": True}
        one.sendall(struct.pack("!I", 101)); refused(d.receive, two, 100)
    finally:
        one.close(); two.close()

    # Cursor files, receipts and socket paths cannot follow a symlink to private data.
    private = Path(folder) / "private.txt"; private.write_text("private canary")
    bad = Path(folder) / "desktop-inputs" / ("e" * 32 + ".json"); bad.symlink_to(private)
    refused(receipts.read, "e" * 32)
    d.socket_path().symlink_to(private); refused(d.connect_daemon)
    assert private.read_text() == "private canary"

# Focus-changing start/new dispatches share the exact lock with sends and viewer inputs.
# The start() function itself stays unlocked because check_ready() calls it inside send's lock.
with tempfile.TemporaryDirectory(prefix="desktop-focus-lock-check-") as folder, \
     patch.object(remote_desktop, "ROOT", Path(folder)):
    lock_path = Path(folder) / "desktop.lock"
    with d.owned_lock(lock_path), patch.object(remote_desktop, "start") as start, \
         patch.object(remote_desktop, "check_ready") as ready:
        for action in ("start", "new"):
            try:
                remote_desktop.dispatch({"action": action})
            except remote_desktop.DesktopError as exc:
                assert exc.not_submitted is True
            else:
                raise AssertionError("Focus action ignored a held viewer input lock")
        assert not start.called and not ready.called
    def prove_locked(*args):
        refused(d.owned_lock(lock_path).__enter__)
        return {"started": True}
    with patch.object(remote_desktop, "start", side_effect=prove_locked):
        assert remote_desktop.dispatch({"action": "start"})["started"]
    button = types.SimpleNamespace(queryAction=lambda: types.SimpleNamespace(doAction=lambda index: prove_locked()))
    with patch.object(remote_desktop, "check_ready", return_value={"mode": "Code"}), \
         patch.object(remote_desktop, "document", return_value=(object(), {})), \
         patch.object(remote_desktop, "find", return_value=button), patch.object(remote_desktop, "showing", return_value=True):
        assert remote_desktop.dispatch({"action": "new"}) == {"opened": True, "mode": "Code"}
    with d.owned_lock(lock_path): pass

# Keys/buttons are always released, including when the press acknowledgement is lost.
native = d.MutterSession.__new__(d.MutterSession)
native.remote_path, native.stream_path, native.geometry = "/owned", "/stream", (0, 0, 1920, 1080)
calls = []
def lost_ack(destination, path, interface, method, signature=None, arguments=()):
    calls.append((method, arguments))
    if method == "NotifyKeyboardKeysym" and arguments == (0xffe3, True):
        raise RuntimeError("Lost acknowledgement")
native.call = lost_ack
try: native.key("Control_L+v")
except RuntimeError: pass
assert calls == [("NotifyKeyboardKeysym", (0xffe3, True)), ("NotifyKeyboardKeysym", (0xffe3, False))]
calls.clear(); native.call = lambda *args: calls.append(args)
native.pointer(1, 1)
assert calls[-1][-1] == ("/stream", 1919, 1079)
calls.clear()
def bad_button(button, state):
    calls.append((button, state))
    if state: raise RuntimeError("Button acknowledgement lost")
native.button = bad_button
try: native.perform(click)
except RuntimeError: pass
assert calls[-1] == (1, False)

# Pasting uses no shell, preserves exact text clipboard bytes, and respects user clipboard changes.
with tempfile.TemporaryDirectory(prefix="desktop-clipboard-check-") as folder, patch.object(d, "ROOT", Path(folder)):
    binary = Path(folder) / "desktop-tools/usr/bin/xclip"; binary.parent.mkdir(parents=True); binary.touch()
    writes, keys = [], []
    payload = "中文\n$(not a shell)".encode()
    with patch.object(d, "clipboard_read", side_effect=[b"previous\nexact", payload, payload]), \
         patch.object(d, "clipboard_owner", side_effect=[31, 31, 32, 32]), \
         patch.object(d.subprocess, "run", side_effect=lambda argv, **kwargs: writes.append((argv, kwargs["input"]))), \
         patch.object(d.time, "sleep"):
        d.paste_text(payload.decode(), keys.append)
    assert keys == ["Control_L+v"] and [item[1] for item in writes] == [payload, b"previous\nexact"]
    assert all(isinstance(argv, list) for argv, _ in writes)
    writes.clear()
    with patch.object(d, "clipboard_read", side_effect=[b"old", payload, b"user changed"]), \
         patch.object(d, "clipboard_owner", side_effect=[31, 31, 32]), \
         patch.object(d.subprocess, "run", side_effect=lambda argv, **kwargs: writes.append(kwargs["input"])), patch.object(d.time, "sleep"):
        d.paste_text(payload.decode(), keys.append)
    assert writes == [payload]
    writes.clear()
    with patch.object(d, "clipboard_read", return_value=None), patch.object(d, "clipboard_owner", return_value=31), \
         patch.object(d.subprocess, "run") as writer:
        refused(d.paste_text, payload.decode(), keys.append)
        assert not writer.called
    with patch.object(d, "clipboard_read", side_effect=[payload, payload]), \
         patch.object(d, "clipboard_owner", side_effect=[0, 0, 32, 32]), patch.object(d, "clipboard_clear") as clear, \
         patch.object(d.subprocess, "run", side_effect=lambda argv, **kwargs: writes.append(kwargs["input"])), patch.object(d.time, "sleep"):
        d.paste_text(payload.decode(), keys.append)
    assert writes == [payload] and clear.call_args.args == (32,)

# Real Unix IPC lifecycle with a synthetic compositor verifies same-UID peers,
# owner-only socket, framed packets, daemon dispatch, close and socket cleanup.
with tempfile.TemporaryDirectory(prefix="desktop-ipc-check-") as folder, patch.object(d, "ROOT", Path(folder)), \
     patch.dict(sys.modules, {"PIL": fake_pil}):
    original_controller = d.StreamController
    def server():
        d.StreamController = lambda: original_controller(lambda: Native(time.time))
        d.serve()
    process = multiprocessing.get_context("fork").Process(target=server)
    process.start()
    try:
        deadline = time.monotonic() + 3
        while not d.socket_path().exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert d.socket_path().exists() and stat.S_IMODE(d.socket_path().stat().st_mode) == 0o600
        frame = d.dispatch({"action": "desktop_frame", "viewer_id": viewer})
        assert frame["mime"] == "image/jpeg"
        receipt = d.dispatch(input_request(viewer, frame, click))
        assert receipt["state"] == "applied"
        assert d.dispatch({"action": "desktop_close", "viewer_id": viewer})["closed"]
        process.join(timeout=3)
        assert not process.is_alive() and process.exitcode == 0 and not d.socket_path().exists()
    finally:
        if process.is_alive():
            process.terminate(); process.join(timeout=2)

print("Desktop stream checks passed: normalized finite coordinates, JPEG/frame/session/viewer guards, shared desktop lock, durable pending-before-input receipts, no unknown replay, clipboard preservation, release-on-error, TTL cleanup and bounded private IPC")
