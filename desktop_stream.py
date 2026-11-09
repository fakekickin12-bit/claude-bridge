#!/usr/bin/env python3
"""Private, bounded GNOME desktop viewer. No RDP login or public listening port."""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path.home() / ".local/share/claude-bridge"
sys.path.insert(0, str(ROOT / "desktop-tools/usr/lib/python3/dist-packages"))
MAX_JPEG = 1_000_000
MAX_EDGE = 1600
MAX_PACKET = 1_500_000
MAX_REQUEST = 100_000
VIEWER_TTL = 30
FRAME_TTL = 45
REMOTE = "org.gnome.Mutter.RemoteDesktop"
CAST = "org.gnome.Mutter.ScreenCast"
HEX32 = re.compile(r"[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")
KEYS = {"Return": (0xff0d,), "Enter": (0xff0d,), "Tab": (0xff09,), "BackSpace": (0xff08,),
        "Delete": (0xffff,), "Escape": (0xff1b,), "Left": (0xff51,), "Up": (0xff52,),
        "Right": (0xff53,), "Down": (0xff54,), "Home": (0xff50,), "End": (0xff57,),
        "Prior": (0xff55,), "Page_Up": (0xff55,), "Next": (0xff56,), "Page_Down": (0xff56,),
        "Shift_L+Tab": (0xffe1, 0xff09), "Alt_L+Tab": (0xffe9, 0xff09), "Super_L": (0xffeb,),
        "Control_L+Shift_L+z": (0xffe3, 0xffe1, ord("z"))}
KEYS.update({"Control_L+" + key: (0xffe3, ord(key)) for key in "acvxzys"})


class StreamError(Exception):
    pass


class InputNotApplied(StreamError):
    """A checked refusal before sending any desktop input event."""


def identifier(value, pattern=HEX32):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StreamError("桌面操作标记无效。")
    return value


def number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise StreamError("桌面坐标或滚动范围无效。")
    return value


def validate_event(event):
    if not isinstance(event, dict):
        raise StreamError("桌面输入格式无效。")
    kind = event.get("kind")
    fields = {"click": {"kind", "x", "y", "button", "clicks"},
              "scroll": {"kind", "x", "y", "delta_x", "delta_y"},
              "drag": {"kind", "x", "y", "to_x", "to_y", "button"},
              "text": {"kind", "text"}, "key": {"kind", "key"}}
    if kind not in fields or set(event) != fields[kind]:
        raise StreamError("桌面输入类型或字段无效。")
    if kind in {"click", "scroll", "drag"}:
        number(event["x"], 0, 1); number(event["y"], 0, 1)
    if kind == "click":
        if type(event["button"]) is not int or event["button"] not in {1, 2, 3} or type(event["clicks"]) is not int or event["clicks"] not in {1, 2}:
            raise StreamError("鼠标按键或点击次数无效。")
    elif kind == "scroll":
        number(event["delta_x"], -1200, 1200); number(event["delta_y"], -1200, 1200)
        if not event["delta_x"] and not event["delta_y"]:
            raise StreamError("滚动距离不能为空。")
    elif kind == "drag":
        number(event["to_x"], 0, 1); number(event["to_y"], 0, 1)
        if type(event["button"]) is not int or event["button"] != 1:
            raise StreamError("拖动仅支持鼠标左键。")
    elif kind == "key":
        if not isinstance(event["key"], str) or event["key"] not in KEYS:
            raise StreamError("此快捷键不在桌面操作列表中。")
    else:
        value = event["text"]
        if not isinstance(value, str) or not value or len(value) > 20000 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise StreamError("桌面输入文字须为 1–20000 字，且不能含控制字符。")
    return event


def input_fingerprint(viewer, session, frame, event):
    return hashlib.sha256(json.dumps([viewer, session, frame, event], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def owned_directory(path):
    if ROOT.is_symlink() or path.is_symlink():
        raise StreamError("桌面查看器目录不安全。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise StreamError("桌面查看器目录不属于当前用户。")
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def owned_lock(path, *, busy="Ubuntu 正在处理其他桌面操作，请稍后再试。"):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a+b") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise StreamError("桌面操作锁不安全。")
        os.fchmod(stream.fileno(), 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StreamError(busy) from None
        yield stream


class Receipts:
    def __init__(self):
        self.directory = owned_directory(ROOT / "desktop-inputs")

    def read(self, rid):
        path = self.directory / (identifier(rid) + ".json")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 4096:
                raise StreamError("桌面输入回执不安全。")
            try:
                record = json.loads(stream.read(4097))
            except ValueError:
                raise StreamError("桌面输入回执不可读，请核对 Ubuntu 画面，不会重试。") from None
        if not isinstance(record, dict) or record.get("request_id") != rid or record.get("state") not in {"pending", "applied", "not_applied", "unknown"}:
            raise StreamError("桌面输入回执无效，不会重试。")
        return record

    def write(self, record):
        path = self.directory / (identifier(record["request_id"]) + ".json")
        if path.is_symlink():
            raise StreamError("桌面输入回执目标不安全。")
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix="input-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(record, ensure_ascii=False).encode())
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def status(self, viewer, rid, fingerprint):
        identifier(viewer); identifier(rid); identifier(fingerprint, HEX64)
        record = self.read(rid)
        if record is None or record.get("viewer_id") != viewer or not hmac.compare_digest(str(record.get("fingerprint", "")), fingerprint):
            return {"request_id": rid, "fingerprint": fingerprint, "state": "unknown", "found": False,
                    "matched": False, "completed": False, "refresh_required": True}
        state = "unknown" if record["state"] == "pending" else record["state"]
        return {"request_id": rid, "fingerprint": fingerprint, "state": state, "found": True, "matched": True,
                "completed": state in {"applied", "not_applied"}, "refresh_required": True,
                **({"reason": record["reason"]} if record.get("reason") else {})}


class MutterSession:
    """One creator D-Bus connection owns a cast of the existing primary monitor."""
    def __init__(self):
        self.pipeline = self.loop = self.thread = self.bus = None
        self.remote_path = self.stream_path = None
        self.subscriptions = []
        self.closed, self.error = False, ""
        self.condition = threading.Condition()
        self.sample = None
        self.node_ready = threading.Event()
        self.node_id = None
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gio, GLib, Gst
            self.Gio, self.GLib, self.Gst = Gio, GLib, Gst
            Gst.init(None)
            for element in ("pipewiresrc", "videorate", "videoconvert", "videoscale", "jpegenc", "appsink"):
                if Gst.ElementFactory.find(element) is None:
                    raise StreamError("Ubuntu 缺少桌面视频组件：" + element)
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.owners = (self.owner(REMOTE), self.owner(CAST))
            self.require_unlocked()
            self.remote_path = self.call(REMOTE, "/org/gnome/Mutter/RemoteDesktop", REMOTE, "CreateSession")[0]
            sid = self.property(REMOTE, self.remote_path, REMOTE + ".Session", "SessionId")
            self.cast_path = self.call(CAST, "/org/gnome/Mutter/ScreenCast", CAST, "CreateSession", "(a{sv})",
                                       ({"remote-desktop-session-id": GLib.Variant("s", sid)},))[0]
            self.stream_path = self.call(CAST, self.cast_path, CAST + ".Session", "RecordMonitor", "(sa{sv})",
                                         ("", {"cursor-mode": GLib.Variant("u", 1)}))[0]
            self.geometry = self.get_geometry()
            self.subscriptions.append(self.bus.signal_subscribe(CAST, CAST + ".Stream", "PipeWireStreamAdded",
                self.stream_path, None, Gio.DBusSignalFlags.NONE, self.on_node))
            self.subscriptions.append(self.bus.signal_subscribe(REMOTE, REMOTE + ".Session", "Closed",
                self.remote_path, None, Gio.DBusSignalFlags.NONE, self.on_closed))
            self.loop = GLib.MainLoop()
            self.thread = threading.Thread(target=self.loop.run, name="bridge-desktop-events", daemon=True)
            self.thread.start()
            self.call(REMOTE, self.remote_path, REMOTE + ".Session", "Start")
            if not self.node_ready.wait(5) or self.node_id is None:
                raise StreamError("Ubuntu 桌面视频流尚未启动，请稍后重试。")
            width, height = self.geometry[2:]
            scale = min(1, MAX_EDGE / max(width, height))
            self.width, self.height = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
            self.pipeline = Gst.parse_launch(
                f"pipewiresrc path={self.node_id} do-timestamp=true keepalive-time=500 always-copy=true ! "
                "video/x-raw,pixel-aspect-ratio=1/1 ! videorate drop-only=false max-rate=2 ! "
                "video/x-raw,framerate=2/1 ! videoconvert ! videoscale ! "
                f"video/x-raw,width={self.width},height={self.height},pixel-aspect-ratio=1/1 ! "
                "jpegenc quality=70 ! appsink name=frames emit-signals=true max-buffers=1 drop=true sync=false")
            self.pipeline.get_by_name("frames").connect("new-sample", self.on_sample)
            self.gst_bus = self.pipeline.get_bus()
            self.gst_bus.add_signal_watch()
            self.gst_bus.connect("message::error", self.on_pipeline_error)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise StreamError("Ubuntu 桌面视频流无法读取。")
        except StreamError:
            self.close(); raise
        except Exception:
            self.close()
            raise StreamError("当前 GNOME 桌面不支持本地共享接口，未启动远程控制。请确认桌面已登录且视频组件可用。") from None

    def call(self, destination, path, interface, method, signature=None, arguments=()):
        result = self.bus.call_sync(destination, path, interface, method,
            self.GLib.Variant(signature, arguments) if signature else None, None,
            self.Gio.DBusCallFlags.NONE, 2000, None)
        return result.unpack() if result is not None else ()

    def owner(self, name):
        return self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner", "(s)", (name,))[0]

    def property(self, destination, path, interface, name):
        return self.call(destination, path, "org.freedesktop.DBus.Properties", "Get", "(ss)", (interface, name))[0]

    def get_geometry(self):
        values = self.property(CAST, self.stream_path, CAST + ".Stream", "Parameters")
        position, size = values.get("position"), values.get("size")
        if (not isinstance(position, (list, tuple)) or len(position) != 2 or not isinstance(size, (list, tuple)) or len(size) != 2
                or any(type(value) is not int for value in (*position, *size))
                or not all(0 < value <= 16384 for value in size)):
            raise StreamError("Ubuntu 未提供可核对的桌面尺寸。")
        return tuple(position) + tuple(size)

    def require_unlocked(self):
        try:
            active = self.call("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver", "GetActive")[0]
        except Exception:
            raise StreamError("无法确认 Ubuntu 锁屏状态，已停止桌面输入。") from None
        if type(active) is not bool or active:
            raise StreamError("Ubuntu 桌面已锁定，请先在 Ubuntu 解锁。")

    def identity(self):
        if self.closed or self.error:
            raise StreamError(self.error or "Ubuntu 桌面共享已关闭。")
        if (self.owner(REMOTE), self.owner(CAST)) != self.owners or self.get_geometry() != self.geometry:
            raise StreamError("Ubuntu 桌面会话或尺寸已变化，请关闭后重新打开桌面。")
        return self.owners, self.geometry

    def on_node(self, connection, sender, path, interface, signal, parameters):
        self.node_id = parameters.unpack()[0]
        self.node_ready.set()

    def on_closed(self, *args):
        self.error = "Ubuntu 已结束本次桌面共享，请重新打开桌面。"
        with self.condition:
            self.condition.notify_all()

    def on_pipeline_error(self, *args):
        self.error = "Ubuntu 桌面视频流已中断，请关闭后重新打开桌面。"
        with self.condition:
            self.condition.notify_all()

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        if buffer.get_size() > 4_000_000:
            self.error = "Ubuntu 桌面图像超过读取上限。"
            return self.Gst.FlowReturn.ERROR
        success, mapped = buffer.map(self.Gst.MapFlags.READ)
        if not success:
            return self.Gst.FlowReturn.ERROR
        try:
            raw = bytes(mapped.data)
        finally:
            buffer.unmap(mapped)
        if len(raw) > MAX_JPEG:
            from PIL import Image
            image = Image.open(io.BytesIO(raw))
            for quality in (50, 30):
                stream = io.BytesIO(); image.save(stream, "JPEG", quality=quality)
                raw = stream.getvalue()
                if len(raw) <= MAX_JPEG:
                    break
        if len(raw) > MAX_JPEG:
            self.error = "Ubuntu 桌面图像超过传输上限。"
            return self.Gst.FlowReturn.ERROR
        with self.condition:
            self.sample = (raw, self.width, self.height, time.time())
            self.condition.notify_all()
        return self.Gst.FlowReturn.OK

    def frame(self, not_before=0):
        deadline = time.monotonic() + 5
        with self.condition:
            while not self.error and (not self.sample or self.sample[3] <= not_before or time.time() - self.sample[3] > 2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StreamError("Ubuntu 桌面画面暂未更新，请稍后重试。")
                self.condition.wait(min(.5, remaining))
            if self.error:
                raise StreamError(self.error)
            return self.sample

    def pointer(self, x, y):
        width, height = self.geometry[2:]
        self.call(REMOTE, self.remote_path, REMOTE + ".Session", "NotifyPointerMotionAbsolute", "(sdd)",
                  (self.stream_path, min(width - 1, x * width), min(height - 1, y * height)))

    def button(self, button, down):
        self.call(REMOTE, self.remote_path, REMOTE + ".Session", "NotifyPointerButton", "(ib)",
                  ({1: 272, 2: 274, 3: 273}[button], down))

    def key(self, key):
        pressed, release_failed = [], False
        try:
            for symbol in KEYS[key]:
                pressed.append(symbol)
                self.call(REMOTE, self.remote_path, REMOTE + ".Session", "NotifyKeyboardKeysym", "(ub)", (symbol, True))
        finally:
            for symbol in reversed(pressed):
                try:
                    self.call(REMOTE, self.remote_path, REMOTE + ".Session", "NotifyKeyboardKeysym", "(ub)", (symbol, False))
                except Exception:
                    release_failed = True
            if release_failed:
                raise StreamError("桌面按键释放未确认，已停止本次控制。")

    def perform(self, event):
        kind = event["kind"]
        if kind in {"click", "scroll", "drag"}:
            self.pointer(event["x"], event["y"])
        if kind == "click":
            for index in range(event["clicks"]):
                try:
                    self.button(event["button"], True)
                finally:
                    self.button(event["button"], False)
                if index == 0 and event["clicks"] == 2:
                    time.sleep(.07)
        elif kind == "scroll":
            self.call(REMOTE, self.remote_path, REMOTE + ".Session", "NotifyPointerAxis", "(ddu)",
                      (float(event["delta_x"]), float(event["delta_y"]), 3))
        elif kind == "drag":
            try:
                self.button(1, True)
                for index in range(1, 13):
                    self.pointer(event["x"] + (event["to_x"] - event["x"]) * index / 12,
                                 event["y"] + (event["to_y"] - event["y"]) * index / 12)
                    time.sleep(.01)
            finally:
                self.button(1, False)
        elif kind == "key":
            self.key(event["key"])
        else:
            paste_text(event["text"], self.key)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.pipeline is not None:
            with contextlib.suppress(Exception):
                self.pipeline.set_state(self.Gst.State.NULL)
        if hasattr(self, "gst_bus"):
            with contextlib.suppress(Exception):
                self.gst_bus.remove_signal_watch()
        if self.bus is not None and self.remote_path:
            with contextlib.suppress(Exception):
                self.call(REMOTE, self.remote_path, REMOTE + ".Session", "Stop")
        if self.bus is not None:
            for subscription in self.subscriptions:
                with contextlib.suppress(Exception):
                    self.bus.signal_unsubscribe(subscription)
        if self.loop is not None:
            self.loop.quit()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)


def clipboard_read(binary):
    process = subprocess.Popen([binary, "-selection", "clipboard", "-out"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    result, deadline = bytearray(), time.monotonic() + 1
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            block = os.read(process.stdout.fileno(), 65536)
            if not block:
                return bytes(result) if process.wait(timeout=.2) == 0 else None
            result.extend(block)
            if len(result) > 1_000_000:
                break
        return None
    finally:
        if process.poll() is None:
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=.2)
        process.stdout.close()


def clipboard_owner():
    try:
        from Xlib import display
        connection = display.Display(os.environ.get("DISPLAY", ":0"))
        try:
            owner = connection.get_selection_owner(connection.intern_atom("CLIPBOARD"))
            return int(getattr(owner, "id", owner) or 0)
        finally:
            connection.close()
    except Exception:
        raise StreamError("无法确认 Ubuntu 剪贴板状态，已停止粘贴。") from None


def clipboard_clear(expected_owner):
    """Restore an originally absent selection only while our paste still owns it."""
    from Xlib import X, display
    from Xlib.protocol import request
    connection = display.Display(os.environ.get("DISPLAY", ":0"))
    try:
        atom = connection.intern_atom("CLIPBOARD")
        owner = connection.get_selection_owner(atom)
        if int(getattr(owner, "id", owner) or 0) == expected_owner:
            request.SetSelectionOwner(display=connection.display, window=X.NONE, selection=atom, time=X.CurrentTime)
            connection.sync()
    finally:
        connection.close()


def paste_text(value, send_key):
    binary = str(ROOT / "desktop-tools/usr/bin/xclip")
    if not Path(binary).is_file():
        raise StreamError("Ubuntu 缺少文字粘贴组件。")
    try:
        previous_owner = clipboard_owner()
        previous = clipboard_read(binary) if previous_owner else None
        if previous_owner and previous is None:
            raise StreamError("当前剪贴板不是可保留的普通文字，已停止粘贴；请先复制一段普通文字后再试。")
        if clipboard_owner() != previous_owner:
            raise StreamError("Ubuntu 剪贴板刚刚发生变化，请稍后再输入文字。")
    except StreamError as exc:
        raise InputNotApplied(str(exc)) from None
    data = value.encode("utf-8")
    def write(raw):
        subprocess.run([binary, "-selection", "clipboard", "-in"], input=raw,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=True)
    write(data)
    pasted_owner = clipboard_owner()
    try:
        if clipboard_read(binary) != data:
            raise StreamError("桌面剪贴板未确认，文字尚未粘贴。")
        send_key("Control_L+v")
        time.sleep(.2)
    finally:
        if clipboard_read(binary) == data and clipboard_owner() == pasted_owner:
            if previous_owner:
                write(previous)
            else:
                clipboard_clear(pasted_owner)


class StreamController:
    def __init__(self, backend_factory=MutterSession, clock=time.time):
        owned_directory(ROOT)
        self.backend_factory, self.clock = backend_factory, clock
        self.backend = None
        self.session_id = uuid.uuid4().hex
        self.viewers, self.grants = {}, {}
        self.last_input = 0
        self.receipts = Receipts()

    def expire(self):
        now = self.clock()
        for viewer in list(self.viewers):
            if now - self.viewers[viewer]["seen"] > VIEWER_TTL:
                self.close(viewer)
        for frame in list(self.grants):
            if self.grants[frame]["expires"] < now:
                self.grants.pop(frame, None)

    def frame(self, viewer):
        identifier(viewer); self.expire()
        if viewer not in self.viewers and len(self.viewers) >= 16:
            raise StreamError("桌面查看设备已达到上限，请先关闭其他查看窗口。")
        if self.backend is None:
            self.backend = self.backend_factory()
            self.session_id = uuid.uuid4().hex
            self.last_input = 0
        identity = self.backend.identity()
        self.backend.require_unlocked()
        raw, width, height, captured_at = self.backend.frame(self.last_input)
        from PIL import Image
        if not raw or len(raw) > MAX_JPEG or max(width, height) > MAX_EDGE or min(width, height) < 2:
            raise StreamError("桌面画面超过传输尺寸上限。")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "JPEG" or image.size != (width, height):
                raise StreamError("桌面图像尺寸校验失败。")
        now = self.clock()
        if not isinstance(captured_at, (int, float)) or not math.isfinite(captured_at) or not -1 <= now - captured_at <= 5:
            raise StreamError("Ubuntu 桌面画面已过期，请刷新。")
        self.viewers.setdefault(viewer, {"clicked": 0})["seen"] = now
        fid = uuid.uuid4().hex
        self.grants[fid] = {"viewer": viewer, "session": self.session_id, "identity": identity,
                            "expires": min(now, captured_at) + FRAME_TTL}
        own = [key for key, value in self.grants.items() if value["viewer"] == viewer]
        for old in own[:-8]:
            self.grants.pop(old, None)
        desktop_width, desktop_height = self.backend.geometry[2:]
        return {"session_id": self.session_id, "frame_id": fid, "captured_at": captured_at,
                "expires_at": self.grants[fid]["expires"], "mime": "image/jpeg", "image_base64": base64.b64encode(raw).decode("ascii"),
                "width": width, "height": height, "desktop_width": desktop_width, "desktop_height": desktop_height}

    def input(self, request):
        viewer = identifier(request.get("viewer_id")); session = identifier(request.get("session_id"))
        frame = identifier(request.get("frame_id")); rid = identifier(request.get("request_id"))
        event = validate_event(request.get("event"))
        fingerprint = input_fingerprint(viewer, session, frame, event)
        saved = self.receipts.read(rid)
        if saved is not None:
            if saved.get("viewer_id") != viewer or not hmac.compare_digest(str(saved.get("fingerprint", "")), fingerprint):
                raise StreamError("该桌面输入编号已用于其他操作，不会再次执行。")
            return self.receipts.status(viewer, rid, fingerprint)
        record = {"request_id": rid, "viewer_id": viewer, "fingerprint": fingerprint,
                  "state": "not_applied", "created_at": self.clock()}
        attempted = False
        try:
            with owned_lock(ROOT / "desktop.lock"):
                self.expire()
                grant = self.grants.get(frame)
                now = self.clock()
                if (self.backend is None or viewer not in self.viewers or not grant or grant["viewer"] != viewer
                        or grant["session"] != session or session != self.session_id or grant["expires"] < now):
                    raise StreamError("画面或桌面会话已过期，请刷新后再操作。")
                if self.backend.identity() != grant["identity"]:
                    raise StreamError("Ubuntu 桌面会话或尺寸已变化，请重新打开桌面。")
                self.backend.require_unlocked()
                if event["kind"] == "text" and (not self.viewers[viewer]["clicked"] or now - self.viewers[viewer]["clicked"] > 120):
                    raise StreamError("请先在桌面画面中点击目标输入框，再发送文字。")
                record["state"] = "pending"
                self.receipts.write(record)
                try:
                    attempted = True
                    self.backend.perform(event)
                except InputNotApplied as exc:
                    record.update(state="not_applied", reason=str(exc))
                except Exception:
                    record.update(state="unknown", reason="桌面输入结果未确认，请查看新画面；不会自动重试。")
                    # Closing this session destroys its own virtual input devices, including
                    # any key/button whose release acknowledgement was lost. Never touch RDP.
                    with contextlib.suppress(Exception):
                        self.backend.close()
                    self.backend = None
                    for current in self.viewers.values():
                        current["clicked"] = 0
                else:
                    record["state"] = "applied"
                    if event["kind"] == "click":
                        self.viewers[viewer]["clicked"] = now
                finally:
                    self.last_input = self.clock()
                    self.grants.clear()
                self.receipts.write(record)
        except StreamError as exc:
            record.update(state="unknown" if attempted else "not_applied",
                          reason="桌面输入结果未确认，请查看新画面；不会自动重试。" if attempted else str(exc))
            self.receipts.write(record)
        return self.receipts.status(viewer, rid, fingerprint)

    def close(self, viewer):
        identifier(viewer)
        self.viewers.pop(viewer, None)
        self.grants = {key: value for key, value in self.grants.items() if value["viewer"] != viewer}
        if not self.viewers and self.backend is not None:
            self.backend.close(); self.backend = None
        return {"closed": True, "viewer_id": viewer}

    def shutdown(self):
        if self.backend is not None:
            self.backend.close(); self.backend = None
        self.grants.clear(); self.viewers.clear()

    def dispatch(self, request):
        action = request.get("action")
        if action == "desktop_frame":
            return self.frame(request.get("viewer_id"))
        if action == "desktop_input":
            return self.input(request)
        if action == "desktop_input_status":
            return self.receipts.status(request.get("viewer_id"), request.get("request_id"), request.get("fingerprint"))
        if action == "desktop_close":
            return self.close(request.get("viewer_id"))
        raise StreamError("不支持的桌面查看器操作。")


def receive(connection, maximum):
    def exact(length):
        chunks = bytearray()
        while len(chunks) < length:
            block = connection.recv(length - len(chunks))
            if not block:
                raise StreamError("桌面查看器连接已中断。")
            chunks.extend(block)
        return bytes(chunks)
    length = struct.unpack("!I", exact(4))[0]
    if not 0 < length <= maximum:
        raise StreamError("桌面查看器数据超过传输上限。")
    return json.loads(exact(length))


def transmit(connection, value, maximum):
    raw = json.dumps(value, ensure_ascii=False).encode()
    if len(raw) > maximum:
        raise StreamError("桌面查看器数据超过传输上限。")
    connection.sendall(struct.pack("!I", len(raw)) + raw)


def socket_path():
    return ROOT / "desktop-stream.sock"


def connect_daemon():
    path = socket_path()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise StreamError("桌面查看器连接文件不安全。")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(15)
    try:
        connection.connect(str(path))
        _pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            raise StreamError("桌面查看器服务不属于当前用户。")
        return connection
    except (FileNotFoundError, ConnectionRefusedError):
        connection.close(); return None
    except Exception:
        connection.close(); raise


def dispatch(request, *, root=None, environment=None):
    global ROOT
    if root is not None:
        ROOT = Path(root)
    owned_directory(ROOT)
    action = request.get("action")
    identifier(request.get("viewer_id"))
    if action == "desktop_input_status":
        return Receipts().status(request["viewer_id"], request.get("request_id"), request.get("fingerprint"))
    connection = connect_daemon()
    if connection is None and action == "desktop_close":
        return {"closed": True, "viewer_id": request["viewer_id"]}
    if connection is None and action == "desktop_input":
        # Never start a new desktop session in response to an input or repeat an unknown request.
        event = validate_event(request.get("event"))
        fingerprint = input_fingerprint(request["viewer_id"], identifier(request.get("session_id")),
                                        identifier(request.get("frame_id")), event)
        receipts = Receipts(); rid = identifier(request.get("request_id"))
        saved = receipts.read(rid)
        if saved is not None:
            if saved.get("viewer_id") != request["viewer_id"] or not hmac.compare_digest(str(saved.get("fingerprint", "")), fingerprint):
                raise StreamError("该桌面输入编号已被使用，不会重试。")
            return receipts.status(request["viewer_id"], rid, fingerprint)
        receipts.write({"request_id": rid, "viewer_id": request["viewer_id"], "fingerprint": fingerprint,
                        "state": "not_applied", "created_at": time.time(), "reason": "桌面共享已关闭，请重新打开画面。"})
        return receipts.status(request["viewer_id"], rid, fingerprint)
    if connection is None:
        if action != "desktop_frame":
            raise StreamError("不支持的桌面查看器操作。")
        try:
            with owned_lock(ROOT / "desktop-stream-start.lock", busy="桌面查看器正在启动，请稍后刷新。"):
                connection = connect_daemon()
                if connection is None:
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--daemon"],
                        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
                    deadline = time.monotonic() + 5
                    while connection is None and time.monotonic() < deadline:
                        time.sleep(.05); connection = connect_daemon()
        except StreamError:
            connection = connect_daemon()
            if connection is None:
                raise
        if connection is None:
            raise StreamError("Ubuntu 桌面查看器未启动，请稍后重试。")
    try:
        transmit(connection, request, MAX_REQUEST)
        response = receive(connection, MAX_PACKET)
    finally:
        connection.close()
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise StreamError(response.get("error", "Ubuntu 桌面操作未完成。") if isinstance(response, dict) else "桌面返回格式无效。")
    return response["result"]


def serve():
    owned_directory(ROOT)
    with owned_lock(ROOT / "desktop-stream-daemon.lock"):
        path = socket_path()
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise StreamError("桌面查看器连接文件不安全。")
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        controller = StreamController()
        try:
            listener.bind(str(path)); path.chmod(0o600); listener.listen(8); listener.settimeout(.5)
            started, ever_opened = time.monotonic(), False
            while True:
                controller.expire()
                if ever_opened and not controller.viewers or not ever_opened and time.monotonic() - started > 15:
                    break
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(15)
                    try:
                        _pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        if uid != os.getuid():
                            raise StreamError("桌面查看器客户端不属于当前用户。")
                        request = receive(connection, MAX_REQUEST)
                        if not isinstance(request, dict):
                            raise StreamError("桌面请求格式无效。")
                        result = controller.dispatch(request)
                        ever_opened = ever_opened or bool(controller.viewers)
                        response = {"ok": True, "result": result}
                    except StreamError as exc:
                        response = {"ok": False, "error": str(exc)}
                    except Exception:
                        response = {"ok": False, "error": "Ubuntu 桌面操作未完成，请刷新画面；不会自动重试输入。"}
                    with contextlib.suppress(Exception):
                        transmit(connection, response, MAX_PACKET)
        finally:
            controller.shutdown(); listener.close()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


if __name__ == "__main__":
    if sys.argv[1:] == ["--daemon"]:
        serve()
    else:
        raise SystemExit("Desktop viewer only runs through Claude Bridge.")
