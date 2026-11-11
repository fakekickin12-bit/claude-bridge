#!/usr/bin/env python3
"""Ubuntu Claude Desktop UI transport. No CDP, token extraction, or app injection."""
from __future__ import annotations

import hashlib
import hmac
import fcntl
import json
import os
import re
import select
import subprocess
import sys
import shutil
import stat
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path.home() / ".local/share/claude-bridge"
TOOLS = ROOT / "desktop-tools"
VERSION = "1.19.0"
STOP_BUTTONS = {"Stop response", "Stop generating", "Stop", "Stop task"}
CODE_LOCAL_SESSION = re.compile(r"/epitaxy/local_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/?", re.IGNORECASE)
CODE_NEW_PATHS = {"/epitaxy", "/epitaxy/"}
sys.path.insert(0, str(TOOLS / "usr/lib/python3/dist-packages"))


class DesktopError(Exception):
    pass


class ClientUnavailable(DesktopError):
    pass


class WindowUnavailable(DesktopError):
    pass


class HiddenX11Window(WindowUnavailable):
    pass


def environment():
    env = os.environ.copy()
    runtime = Path("/run/user") / str(os.getuid())
    env.update(XDG_RUNTIME_DIR=str(runtime), DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(runtime / "bus"), DISPLAY=":0")
    auth = list(runtime.glob(".mutter-Xwaylandauth.*"))
    if auth:
        env["XAUTHORITY"] = str(auth[0])
    env["LD_LIBRARY_PATH"] = str(TOOLS / "usr/lib/x86_64-linux-gnu")
    return env


def initialize():
    os.environ.update(environment())
    import pyatspi
    return pyatspi


def pump_events():
    from gi.repository import GLib
    context = GLib.MainContext.default()
    for _ in range(100):
        if not context.pending():
            break
        context.iteration(False)


def walk(node, depth=0, prune_roles=(), prune_feeds=False):
    if depth > 65:
        return
    yield node
    if prune_roles and node.getRoleName() in prune_roles:
        return
    if prune_feeds and node.getRoleName() in {"panel", "landmark"} and (
            node.name == "Chat messages" or "feed" in node_attributes(node).get("xml-roles", "").split()):
        return
    try:
        children = [node.getChildAtIndex(i) for i in range(min(node.childCount, 3000))]
    except Exception:
        return
    for child in children:
        if child:
            yield from walk(child, depth + 1, prune_roles, prune_feeds)


def role_nodes(root, roles):
    """Use the native subtree query, retaining recursive traversal as a fallback."""
    try:
        collection = root.queryCollection()
        atspi = initialize()
        from gi.repository import Atspi
        constants = {"document web": atspi.ROLE_DOCUMENT_WEB, "entry": atspi.ROLE_ENTRY,
                     "push button": atspi.ROLE_PUSH_BUTTON, "dialog": atspi.ROLE_DIALOG,
                     "alert dialog": atspi.ROLE_ALERT, "menu": atspi.ROLE_MENU,
                     "file chooser": atspi.ROLE_FILE_CHOOSER}
        rule = collection.createMatchRule(
            Atspi.StateSet.new([]), Atspi.CollectionMatchType.ALL, [], Atspi.CollectionMatchType.ALL,
            [int(constants[role]) for role in roles], Atspi.CollectionMatchType.ANY,
            [], Atspi.CollectionMatchType.ALL, False)
        nodes = list(collection.getMatches(rule, Atspi.CollectionSortOrder.CANONICAL, 0, True))
        if root.getRoleName() in roles and root not in nodes:
            nodes.insert(0, root)
        return [node for node in nodes if node.getRoleName() in roles]
    except Exception:
        return [node for node in walk(root) if node.getRoleName() in roles]


def app():
    atspi = initialize()
    pump_events()
    apps = [a for a in atspi.Registry.getDesktop(0) if a.name == "claude-desktop"]
    if not apps:
        raise ClientUnavailable("Claude 桌面客户端尚未运行，请点击“打开 / 显示客户端”。")
    if len(apps) != 1:
        raise DesktopError("未找到唯一的 Claude 桌面客户端，请在 Ubuntu 打开该客户端。")
    from gi.repository import Atspi
    apps[0].set_cache_mask(Atspi.Cache(0))
    apps[0].clear_cache()
    return apps[0]


def find(role, name, root=None):
    nodes = []
    for node in role_nodes(root if root is not None else app(), (role,)):
        try:
            if node.getRoleName() == role and node.name == name:
                nodes.append(node)
        except Exception:
            pass
    if len(nodes) != 1:
        raise DesktopError("客户端控件尚未就绪：" + name)
    return nodes[0]


def showing(node):
    return node.getState().contains(initialize().STATE_SHOWING)


def code_page(attrs):
    path = urlsplit(attrs["URI"]).path
    return path == "/epitaxy" or path.startswith("/epitaxy/")


def code_panel(page):
    panels = [n for n in walk(page, prune_roles=("article",), prune_feeds=True)
              if "epitaxy-chat-panel" in node_classes(n) and showing(n)]
    if len(panels) != 1:
        raise DesktopError("请在 Ubuntu 的 Claude Code 中显示唯一的主会话面板。")
    return panels[0]


def composer(page=None, attrs=None):
    if page is None:
        page, attrs = document()
    is_code = code_page(attrs)
    root = code_panel(page) if is_code else page
    entry = find("entry", "Prompt" if is_code else "Write your prompt to Claude", root)
    if not showing(entry):
        raise DesktopError("Claude 输入框当前不可见。请在 Ubuntu 打开目标会话。")
    return entry


def code_dock(entry):
    node = entry
    while node is not None:
        if "group/approval-dock" in node_classes(node):
            return node
        node = node.parent
    raise DesktopError("Claude Code 的输入区尚未就绪。请检查当前会话。")


def new_code_target(attrs, controls):
    if urlsplit(attrs["URI"]).path not in CODE_NEW_PATHS:
        return None
    visible = [n for n in buttons(controls) if showing(n)]
    environments = [n for n in visible if n.name.startswith("Environment: ")]
    folders = [n for n in visible if n.name.startswith("Folder: ")]
    if len(environments) != 1 or environments[0].name != "Environment: This computer" or len(folders) != 1:
        return None
    folder = Path(folders[0].name[len("Folder: "):]).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        return None
    return {"workspace": str(folder.resolve()),
            "selector_ids": [node_attributes(n).get("id", "") for n in (environments[0], folders[0])]}


def conversation_context(process_id, uri, new_target=None):
    identity = str(process_id) + "|" + uri
    if urlsplit(uri).path in CODE_NEW_PATHS:
        identity += "|" + json.dumps(new_target, sort_keys=True)
    return hashlib.sha256(identity.encode()).hexdigest()


def code_approval_pending(dock, entry):
    # Code renders permission/questions inline beside the composer, rather than in dialogs.
    for child in dock:
        if child is None or not showing(child):
            continue
        nodes = list(walk(child))
        if entry in nodes:
            continue
        if "z-0" in node_classes(child) and any(n.name.startswith("Model: ") for n in buttons(child)):
            continue
        if "w-full" in node_classes(child):
            controls = [n for n in nodes if n.getRoleName() in {
                "push button", "toggle button", "radio button", "check box", "entry"} and showing(n)]
            names = [n.name for n in controls]
            if (any(name.startswith("Environment: ") for name in names)
                    and any(name.startswith("Model: ") for name in names)
                    and all(n.getRoleName() == "push button" and n.name.startswith(
                        ("Environment: ", "Folder: ", "Model: ", "Permissions: ", "Usage: ")) for n in controls)):
                continue
        if any(n.getRoleName() in {"push button", "toggle button", "radio button", "check box", "entry"}
               and showing(n) for n in nodes):
            return True
    return False


def text(node):
    try:
        raw = node.queryText().getText(0, -1)
    except Exception:
        return ""
    if "\ufffc" in raw:
        child_nodes = [child for child in node if child]
        # Chromium represents contenteditable blocks as adjacent embedded objects.
        # Paragraph boundaries are real newlines, even when absent in the parent's AX text.
        if raw and all(c == "\ufffc" for c in raw) and child_nodes and all(c.getRoleName() == "paragraph" for c in child_nodes):
            return "\n".join("" if text(c) == "\n" else text(c) for c in child_nodes)
        children = iter(child_nodes)
        result = []
        for char in raw:
            if char == "\ufffc":
                child = next(children, None)
                result.append(text(child) if child else "")
            else:
                result.append(char)
        return "".join(result)
    return raw


def buttons(root=None):
    result = []
    for node in role_nodes(root if root is not None else app(), ("push button",)):
        try:
            if node.getRoleName() == "push button":
                result.append(node)
        except Exception:
            pass
    return result


def xcommand(*args):
    p = subprocess.run([str(TOOLS / "usr/bin/xdotool"), *map(str, args)], env=environment(),
                       capture_output=True, text=True, timeout=8)
    if p.returncode:
        if args[0] == "search" and p.returncode == 1 and not p.stderr.strip():
            raise WindowUnavailable("Claude 主窗口当前未显示，请点击“打开 / 显示客户端”恢复窗口。")
        # Record only the fixed operation name and tool diagnostics, never text/clipboard data.
        try:
            import remote
            remote.atomic(ROOT / "last-window-error.json", {"operation": args[0], "returncode": p.returncode,
                "diagnostic": p.stderr[:1200], "time": time.time()})
        except Exception:
            pass
        raise DesktopError("Ubuntu 桌面输入操作未完成（" + str(args[0]) + "），已停止提交。请检查客户端窗口与桌面状态。")
    return p.stdout.strip()


def window_id():
    pid = app().get_process_id()
    # Read the window manager's client list instead of walking every transient X window.
    # A clipboard or tooltip window disappearing during xdotool search can abort that search.
    from Xlib import X, display, error
    try:
        connection = display.Display(environment()["DISPLAY"])
    except Exception:
        raise DesktopError("无法连接 Ubuntu 图形桌面，请确认桌面已登录且可用。") from None
    found, owned = [], 0
    try:
        root = connection.screen().root
        clients = root.get_full_property(connection.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
        for wid in clients.value if clients is not None else []:
            try:
                window = connection.create_resource_object("window", int(wid))
                owner = window.get_full_property(connection.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
                classes = window.get_wm_class() or ()
                if owner is not None and len(owner.value) and int(owner.value[0]) == pid and any("claude" in c.lower() for c in classes):
                    owned += 1
                    if window.get_attributes().map_state == X.IsViewable:
                        found.append(int(wid))
            except error.BadWindow:
                continue
    finally:
        connection.close()
    if not found:
        if owned:
            raise HiddenX11Window("Claude 主窗口当前未显示，请点击“打开 / 显示客户端”恢复窗口。")
        raise WindowUnavailable("Bridge 未找到可输入的 Claude 窗口。请点击“打开 / 显示客户端”；若仍失败，请在 Ubuntu 完全退出 Claude 后再从 Bridge 打开。")
    if len(found) != 1:
        raise DesktopError("未找到唯一的 Claude 桌面窗口。")
    return int(found[0])


def desktop_locked():
    from gi.repository import Gio
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return bool(bus.call_sync("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver",
            "GetActive", None, None, 0, 1500, None).unpack()[0])
    except Exception:
        return False


def native_input_frame():
    """Bind the visible page to its own active AT-SPI frame, without X11 IDs."""
    application = app()
    page, attrs = document(application)
    frame = page
    for _ in range(40):
        if frame is None:
            break
        if frame.getRoleName() == "frame":
            break
        frame = frame.parent
    else:
        frame = None
    if frame is None or frame.getRoleName() != "frame":
        raise DesktopError("尚未确认 Claude 当前页面所属窗口，请在 Ubuntu 显示当前会话。")
    if hasattr(frame, "clear_cache"):
        frame.clear_cache()
    pid = application.get_process_id()
    if frame.get_process_id() != pid or not showing(frame):
        raise DesktopError("Claude 当前页面所属窗口已变化，已停止输入。")
    if not frame.getState().contains(initialize().STATE_ACTIVE):
        raise DesktopError("请在 Ubuntu 点击当前 Claude 窗口，让它处于前台后再发送。")
    return {"kind": "wayland", "frame_id": accessible_key(frame), "process_id": pid,
            "context": page_context(application, page, attrs)}


def input_window():
    try:
        return window_id()
    except HiddenX11Window:
        raise
    except WindowUnavailable:
        return native_input_frame()


class NativeKeyboard:
    """A short-lived GNOME keyboard session; no screen stream or app restart."""
    REMOTE = "org.gnome.Mutter.RemoteDesktop"
    KEYS = {"Control_L+v": (0xffe3, ord("v")), "Return": (0xff0d,)}

    def __init__(self):
        initialize()
        from gi.repository import Gio, GLib
        self.Gio, self.GLib, self.path = Gio, GLib, None
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.owner = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                               "GetNameOwner", "(s)", (self.REMOTE,))[0]
        self.require_unlocked()
        try:
            self.path = self.call(self.REMOTE, "/org/gnome/Mutter/RemoteDesktop", self.REMOTE, "CreateSession")[0]
            self.call(self.REMOTE, self.path, self.REMOTE + ".Session", "Start")
        except Exception:
            self.close()
            raise DesktopError("Ubuntu 原生键盘通道尚未就绪，尚未输入或提交；请稍后重试。") from None

    def call(self, destination, path, interface, method, signature=None, arguments=()):
        result = self.bus.call_sync(destination, path, interface, method,
            self.GLib.Variant(signature, arguments) if signature else None, None,
            self.Gio.DBusCallFlags.NONE, 2000, None)
        return result.unpack() if result is not None else ()

    def require_unlocked(self):
        try:
            active = self.call("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver", "GetActive")[0]
        except Exception:
            raise DesktopError("无法确认 Ubuntu 锁屏状态，已停止原生输入。") from None
        if active is not False:
            raise DesktopError("Ubuntu 桌面已锁定，请先解锁。")

    def key(self, name):
        symbols = self.KEYS[name]
        owner = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "GetNameOwner", "(s)", (self.REMOTE,))[0]
        if owner != self.owner or self.path is None:
            raise DesktopError("Ubuntu 原生键盘会话已变化，已停止输入。")
        self.require_unlocked()
        pressed, release_failed = [], False
        try:
            for symbol in symbols:
                pressed.append(symbol)
                self.call(self.REMOTE, self.path, self.REMOTE + ".Session", "NotifyKeyboardKeysym", "(ub)", (symbol, True))
        finally:
            for symbol in reversed(pressed):
                try:
                    self.call(self.REMOTE, self.path, self.REMOTE + ".Session", "NotifyKeyboardKeysym", "(ub)", (symbol, False))
                except Exception:
                    release_failed = True
            if release_failed:
                self.close()
                raise DesktopError("原生按键释放未确认，已关闭本次键盘通道；请核对 Ubuntu，不会自动重发。")

    def close(self):
        if self.path is not None:
            try:
                self.call(self.REMOTE, self.path, self.REMOTE + ".Session", "Stop")
            except Exception:
                pass
            self.path = None


def require_native_focus(target, context, draft, *, allow_running=False):
    """Slow status checks precede the final exact focused-entry check."""
    current = status()
    require_attachment_draft(current, context, [], draft.strip(), allow_running=allow_running)
    latest = native_input_frame()
    if latest != target or latest["context"] != context or current.get("process_id") != target["process_id"]:
        raise DesktopError("Ubuntu 原生输入目标已变化，已保留草稿。")
    entry = composer()
    if (not entry.getState().contains(initialize().STATE_FOCUSED) or not showing(entry)
            or text(entry).strip() != draft.strip() or visible_menus()):
        raise DesktopError("Claude 输入框未保持焦点或草稿已变化，已停止原生输入。")


def activate():
    initialize()
    if desktop_locked():
        raise DesktopError("Ubuntu 桌面已锁定，请先在 Ubuntu 解锁后再发送。")
    wid = input_window()
    if isinstance(wid, dict):
        return wid
    from Xlib import X, display, error, protocol
    connection = display.Display(environment()["DISPLAY"])
    try:
        root = connection.screen().root
        atom = connection.intern_atom("_NET_ACTIVE_WINDOW")
        event = protocol.event.ClientMessage(window=wid, client_type=atom, data=(32, [2, X.CurrentTime, 0, 0, 0]))
        root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        connection.flush()
        # GNOME updates its active-window property asynchronously after restoring a window.
        # Wait for confirmation instead of treating an unset property as an immediate failure.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = root.get_full_property(atom, X.AnyPropertyType)
            if current is not None and len(current.value) and int(current.value[0]) == wid:
                return wid
            time.sleep(.05)
    except error.BadWindow:
        raise WindowUnavailable("Claude 窗口在恢复时关闭，请重新打开客户端。") from None
    finally:
        connection.close()
    raise DesktopError("Claude 桌面窗口未获得焦点，已停止输入。请先切换到 Ubuntu 的 Claude 窗口。")


def visible_menus():
    atspi = initialize()
    result = []
    for node in role_nodes(app(), ("menu",)):
        try:
            if node.getRoleName() == "menu" and node.getState().contains(atspi.STATE_SHOWING):
                result.append(node)
        except Exception:
            pass
    return result


def focus_composer(expected_context=None, expected_draft="", *, allow_running=False):
    """Focus the actual visible input, including dismissing a native menu grab."""
    from Xlib import X, display
    from Xlib.ext import xtest
    wid = activate()
    if isinstance(wid, dict):
        if not expected_context or wid["context"] != expected_context or visible_menus():
            raise DesktopError("Claude 原生输入目标或菜单状态尚未确认，已停止输入。")
        entry = composer()
        if not entry.getState().contains(initialize().STATE_FOCUSED):
            if not entry.queryComponent().grabFocus():
                raise DesktopError("Claude 输入框未获得焦点，请在 Ubuntu 点击当前输入框。")
        require_native_focus(wid, expected_context, expected_draft, allow_running=allow_running)
        return wid
    connection = display.Display(environment()["DISPLAY"])
    try:
        root = connection.screen().root
        pointer = root.query_pointer()
        held = X.ShiftMask | X.ControlMask | X.Mod1Mask | X.Mod4Mask | X.Mod5Mask | X.Button1Mask | X.Button2Mask | X.Button3Mask
        if pointer.mask & held:
            raise DesktopError("Ubuntu 当前有修饰键或鼠标按键按下，请松开后再发送。尚未提交消息。")
        if visible_menus():
            xcommand("key", "Escape")
            for _ in range(20):
                pump_events()
                if not visible_menus():
                    break
                time.sleep(.05)
            else:
                raise DesktopError("Claude 原生菜单仍在打开，请先关闭菜单。尚未提交消息。")
        entry = composer()
        ext = entry.queryComponent().getExtents(0)
        x, y = int(ext.x + ext.width // 2), int(ext.y + ext.height // 2)
        screen = connection.screen()
        if ext.width < 8 or ext.height < 8 or not (0 <= x < screen.width_in_pixels and 0 <= y < screen.height_in_pixels):
            raise DesktopError("Claude 输入框当前不可见，请先将客户端窗口显示在桌面内。")
        # grabFocus() can report success while an Electron native menu owns keyboard input.
        # A normal pointer click dismisses that grab and gives the web input real focus.
        xtest.fake_input(connection, X.MotionNotify, x=x, y=y)
        xtest.fake_input(connection, X.ButtonPress, 1)
        xtest.fake_input(connection, X.ButtonRelease, 1)
        connection.sync()
        for _ in range(25):
            pump_events()
            entry = composer()
            if entry.getState().contains(initialize().STATE_FOCUSED) and not visible_menus():
                break
            time.sleep(.05)
        else:
            raise DesktopError("Claude 输入框未获得实际焦点。尚未提交消息。")
        if expected_context:
            current = status()
            if current["context"] != expected_context:
                raise DesktopError("聚焦后发现客户端已切换对话。尚未提交消息，请检查目标对话。")
            if (current["draft"] != expected_draft or current["dialogs"]
                    or current["busy"] and not allow_running
                    or current["approval"] or current.get("awaiting_input", False)
                    or current.get("locked", False)):
                raise DesktopError("聚焦时客户端已有草稿、弹窗或任务。已停止输入。")
        time.sleep(.15)
        return wid
    finally:
        current_pointer = connection.screen().root.query_pointer()
        if 'x' in locals() and (current_pointer.root_x, current_pointer.root_y) == (x, y):
            xtest.fake_input(connection, X.MotionNotify, x=pointer.root_x, y=pointer.root_y)
            connection.sync()
        connection.close()


def drop_files(paths, expected_context=None):
    """Use the standard native X11 XDND file-drop protocol, with a private selection."""
    from Xlib import X, Xatom, display, protocol
    wid = focus_composer(expected_context)
    entry = composer()
    ext = entry.queryComponent().getExtents(0)
    x, y = int(ext.x + max(8, ext.width // 2)), int(ext.y + max(8, ext.height // 2))
    if ext.width < 10 or ext.height < 10:
        raise DesktopError("客户端输入框不可见，无法载入附件。")
    connection = display.Display(environment()["DISPLAY"])
    target = connection.create_resource_object("window", wid)
    screen = connection.screen()
    source = screen.root.create_window(0, 0, 1, 1, 0, screen.root_depth, X.InputOutput, X.CopyFromParent,
                                       event_mask=X.PropertyChangeMask)
    atom = connection.intern_atom
    uri = atom("text/uri-list")
    action = atom("XdndActionCopy")
    selection = atom("XdndSelection")
    data = ("\r\n".join(Path(p).as_uri() for p in paths) + "\r\n").encode("utf-8")
    source.set_selection_owner(selection, X.CurrentTime)
    source.change_property(atom("XdndAware"), Xatom.ATOM, 32, [5])
    def send(name, words):
        target.send_event(protocol.event.ClientMessage(window=wid, client_type=atom(name), data=(32, words)), event_mask=0)
        connection.flush()
    accepted, dropped, finished = False, False, False
    next_position = time.monotonic() + .3
    try:
        send("XdndEnter", [source.id, 5 << 24, uri, 0, 0])
        send("XdndPosition", [source.id, 0, (x << 16) | (y & 0xffff), X.CurrentTime, action])
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if not dropped and time.monotonic() >= next_position:
                send("XdndPosition", [source.id, 0, (x << 16) | (y & 0xffff), X.CurrentTime, action])
                next_position = time.monotonic() + .3
            if not connection.pending_events():
                select.select([connection.fileno()], [], [], .15)
                continue
            event = connection.next_event()
            if event.type == X.SelectionRequest:
                prop = event.property or event.target
                if event.target == uri:
                    event.requestor.change_property(prop, uri, 8, data)
                elif event.target == atom("TARGETS"):
                    event.requestor.change_property(prop, Xatom.ATOM, 32, [uri])
                else:
                    prop = X.NONE
                event.requestor.send_event(protocol.event.SelectionNotify(time=event.time, requestor=event.requestor,
                    selection=event.selection, target=event.target, property=prop), event_mask=0)
                connection.flush()
            elif event.type == X.ClientMessage and event.client_type == atom("XdndStatus"):
                accepted = bool(event.data[1][1] & 1)
                if accepted and not dropped:
                    send("XdndDrop", [source.id, 0, X.CurrentTime, 0, 0])
                    dropped = True

            elif event.type == X.ClientMessage and event.client_type == atom("XdndFinished"):
                finished = bool(event.data[1][1] & 1)
                break
        if not (accepted and dropped and finished):
            raise DesktopError("附件载入结果未确认，请先查看 Ubuntu 客户端，避免重复发送。")
        return {"accepted": True, "files": len(paths)}
    finally:
        if not dropped:
            send("XdndLeave", [source.id, 0, 0, 0, 0])
        source.destroy()
        connection.close()


def warm_document_tree(application):
    """Wake lazy Electron document exports without entering any web history."""
    remaining = [384]
    def visit(node, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 24:
            return
        role = node.getRoleName()
        if role in {"document web", "article", "paragraph", "static", "text", "entry"}:
            return
        # Touching the native shell's children makes cold Electron WebContents
        # join the Collection index. Never choose a document from this walk:
        # the complete native role query still enforces URI and uniqueness.
        count = node.childCount
        for index in range(min(count, remaining[0])):
            child = node.getChildAtIndex(index)
            if child is not None:
                visit(child, depth + 1)
    visit(application)


def document(application=None):
    if application is None:
        application = app()
    deadline = time.monotonic() + 4
    warmed = False
    while True:
        pump_events()
        pages = []
        for node in role_nodes(application, ("document web",)):
            try:
                if node.getRoleName() == "document web" and showing(node):
                    if hasattr(node, "clear_cache"):
                        node.clear_cache()
                    attrs = dict(item.split(":", 1) for item in node.queryDocument().getAttributes())
                    uri = urlsplit(attrs.get("URI", ""))
                    if uri.scheme == "https" and uri.netloc == "claude.ai":
                        pages.append((node, attrs))
            except Exception:
                pass
        if len(pages) == 1:
            return pages[0]
        if not pages and not warmed:
            warmed = True
            try:
                warm_document_tree(application)
            except Exception:
                pass
        if time.monotonic() >= deadline:
            break
        time.sleep(.15)
    if pages:
        raise DesktopError("检测到多个 Claude 页面，请在 Ubuntu 只显示要控制的会话。")
    raise DesktopError("暂时读不到 Claude 的当前页面。请让 Ubuntu 上要控制的 Chat 或 Code 会话保持可见，再试一次。")


def node_attributes(node):
    try:
        return dict(item.split(":", 1) for item in node.getAttributes() if ":" in item)
    except Exception:
        return {}


def node_classes(node):
    return node_attributes(node).get("class", "").split()


def native_attachment_controls(controls, is_code=False):
    """Return only exact, visible named attachment-removal controls."""
    root = controls
    if is_code:
        shelves = [n for n in walk(controls) if "epitaxy-attachment-shelf" in node_classes(n) and showing(n)]
        if len(shelves) > 1:
            raise DesktopError("客户端存在多个附件区，已停止自动操作。")
        if not shelves:
            return {}
        root = shelves[0]
    result = {}
    for control in walk(root):
        if control.getRoleName() != "push button" or not showing(control) or not control.name.startswith("Remove "):
            continue
        name = control.name[len("Remove "):]
        path_label = is_code and (name.startswith("~/") or Path(name).is_absolute())
        if not name or not path_label and name != Path(name).name or re.search(r"[\x00-\x1f\x7f]", name):
            raise DesktopError("客户端附件名称尚未可靠识别，请在 Ubuntu 检查。")
        if name in result:
            raise DesktopError("客户端存在同名附件，已停止自动操作。")
        result[name] = control
    return result


def attachment_support_signature(process_id, entry, controls):
    """Current executable/AX identity, not proof that any attachment was loaded."""
    additions = [n for n in buttons(controls) if showing(n) and n.name == "Add"]
    if len(additions) != 1:
        return None
    add_attrs = node_attributes(additions[0])
    entry_attrs = node_attributes(entry)
    if (entry.getRoleName() != "entry" or entry.name != "Prompt"
            or not {"tiptap", "ProseMirror"}.issubset(node_classes(entry))
            or add_attrs.get("haspopup") != "menu"):
        return None
    try:
        process_executable = Path("/proc") / str(process_id) / "exe"
        executable = Path(os.readlink(process_executable))
        # Package updates can unlink the old executable while its process and
        # window remain valid. Stat the live /proc handle, not its stale name.
        metadata = process_executable.stat()
    except OSError:
        return None
    return {"schema": 1, "mode": "Code", "executable": str(executable),
            "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns,
            "entry_tag": entry_attrs.get("tag", ""), "add_tag": add_attrs.get("tag", ""),
            "remove_prefix": "Remove ", "code_shelf": "epitaxy-attachment-shelf"}


def attachment_transport_probe(process_id):
    """Read only the unique current Claude window's owner and XDND support.

    This is a transport preflight, not an attachment receipt. Each delivery must
    still complete XDND and confirm its exact native chips before submission.
    """
    try:
        from Xlib import X, Xatom, display, protocol
        from Xlib.ext import xtest
        if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
            raise DesktopError("Claude 进程身份尚未确认，已停止附件载入。")
        wid = window_id()
        connection = display.Display(environment()["DISPLAY"])
        try:
            window = connection.create_resource_object("window", wid)
            owner = window.get_full_property(connection.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
            classes = window.get_wm_class() or ()
            if (window.get_attributes().map_state != X.IsViewable or owner is None
                    or len(owner.value) != 1 or int(owner.value[0]) != process_id
                    or not any("claude" in name.lower() for name in classes)):
                raise DesktopError("Claude 附件目标窗口已经变化，请到 Ubuntu 查看后重试。")
            aware = window.get_full_property(connection.intern_atom("XdndAware"), X.AnyPropertyType)
            if (aware is None or aware.property_type != Xatom.ATOM or aware.format != 32
                    or len(aware.value) != 1 or not 3 <= int(aware.value[0]) <= 0xffffffff):
                raise DesktopError("当前 Claude 窗口尚未提供兼容的文件拖放接口，请到 Ubuntu 查看。")
            if not connection.has_extension("XTEST"):
                raise DesktopError("Ubuntu 当前桌面缺少附件输入所需的 X11 扩展，请到 Ubuntu 查看。")
            # Importing these objects above confirms that both focus and XDND
            # protocol modules used by drop_files are available, without input.
            if not callable(protocol.event.ClientMessage) or not callable(xtest.fake_input):
                raise DesktopError("Ubuntu 附件传输组件尚未就绪，请到 Ubuntu 查看。")
            return {"ready": True, "reason": ""}
        finally:
            connection.close()
    except DesktopError as exc:
        return {"ready": False, "reason": str(exc)}
    except Exception:
        return {"ready": False, "reason": "Ubuntu 当前桌面的文件拖放传输不可用，请到 Ubuntu 查看。"}


def native_attachment_capabilities(process_id, is_code, entry, controls, visible):
    if not visible:
        return {"native_attachments": False, "attachment_reason": "Ubuntu Claude 窗口当前不可见，请先打开客户端。"}
    if not is_code:
        return {"native_attachments": True}
    try:
        signature = attachment_support_signature(process_id, entry, controls)
    except Exception:
        signature = None
    if signature is None:
        return {"native_attachments": False,
                "attachment_reason": "当前 Code 输入区或添加附件控件尚未可靠识别，请到 Ubuntu 查看。"}
    transport = attachment_transport_probe(process_id)
    if not transport["ready"]:
        return {"native_attachments": False, "attachment_reason": transport["reason"]}
    return {"native_attachments": True}


def match_native_attachments(names, paths, partial=False):
    if len(names) != len(set(names)) or len(paths) != len(set(paths)):
        raise DesktopError("客户端附件名称不唯一，已停止附件操作。")
    matches = {}
    for name in names:
        if name.startswith("~/") or Path(name).is_absolute():
            candidates = [path for path in paths if Path(name).expanduser() == Path(path)]
        else:
            candidates = [path for path in paths if name == Path(path).name]
        if len(candidates) != 1 or candidates[0] in matches:
            raise DesktopError("客户端附件与本次清单不符，已停止操作并保留草稿。")
        matches[candidates[0]] = name
    if not partial and len(matches) != len(paths):
        raise DesktopError("客户端附件尚未完整载入，已保留草稿。")
    return matches


def require_attachment_draft(state, context, paths, draft, partial=False, *, allow_running=False):
    if (state["context"] != context or state["draft"] != draft or state["dialogs"]
            or state["busy"] and not (allow_running and not paths)
            or state["approval"] or state.get("awaiting_input", False) or state.get("locked", False)
            or not state.get("window_visible", True)):
        raise DesktopError("客户端对话、草稿或任务状态已经改变，已停止附件操作。")
    if not partial and state.get("attachment_pending"):
        raise DesktopError("客户端附件仍在载入，已停止提交。")
    return match_native_attachments(state["attachments"], paths, partial)


def project_selection_id(kind, *parts):
    return hashlib.sha256(json.dumps([kind, *parts], ensure_ascii=False).encode()).hexdigest()


def page_context(application, page, attrs):
    target = None
    if code_page(attrs) and urlsplit(attrs["URI"]).path in CODE_NEW_PATHS:
        target = new_code_target(attrs, code_dock(composer(page, attrs)))
    return conversation_context(application.get_process_id(), attrs["URI"], target)


def confirm_page_context(expected):
    application = app()
    page, attrs = document(application)
    if page_context(application, page, attrs) != expected:
        raise DesktopError("Ubuntu 当前会话已经变化，请刷新后重试。")


def match_current(request):
    """Identify the open native task without navigating or requiring an idle composer."""
    application = app()
    page, attrs = document(application)
    route = urlsplit(attrs["URI"]).path
    if not code_page(attrs) or not CODE_LOCAL_SESSION.fullmatch(route):
        raise DesktopError("请先在 Ubuntu 的 Claude Code 中打开要匹配的已有本机会话。")
    context = page_context(application, page, attrs)
    atspi = initialize()
    active = []
    # A single role query avoids walking reply bodies and never reads other task titles.
    for row in buttons(page):
        row_attrs = node_attributes(row)
        if "hit-row-main" not in row_attrs.get("class", "").split() or not showing(row):
            continue
        if not (row_attrs.get("current") == "page" or row.getState().contains(atspi.STATE_SELECTED)):
            continue
        group, sidebar, owner = None, None, row.parent
        while owner is not None and owner != page:
            classes = node_classes(owner)
            if group is None and "group/section" in classes:
                group = owner
            if "dframe-sidebar" in classes:
                sidebar = owner
                break
            owner = owner.parent
        if sidebar is not None and showing(sidebar):
            active.append((row, group))
    if len(active) > 1:
        raise DesktopError("Claude Code 当前会话标记不唯一，请在 Ubuntu 确认只打开一个目标会话后重试。")
    project_title = ""
    if active:
        row, group = active[0]
        title = row.name.strip()
        if not title:
            raise DesktopError("Claude Code 当前会话名称尚未载入，请稍后重试。")
        if group is not None:
            headers = [node for node in buttons(group)
                       if "group/label" in node_classes(node) and showing(node)]
            # Read metadata only when the owner is unique, never guess a nearby project.
            if len(headers) == 1:
                project_title = headers[0].name.strip()
        row_attrs = node_attributes(row)
        if not showing(row) or not (row_attrs.get("current") == "page"
                                   or row.getState().contains(atspi.STATE_SELECTED)) or row.name.strip() != title:
            raise DesktopError("Claude Code 当前会话标记正在变化，请稍后重试。")
    else:
        # A collapsed sidebar need not block a read: the document title belongs to
        # this exact native route. Generic titles cannot identify a task reliably.
        title = attrs.get("Title", "").strip()
        for suffix in (" – Claude Code", " — Claude Code", " - Claude Code"):
            if title.endswith(suffix):
                title = title[:-len(suffix)].strip()
                break
        if not title or title.casefold() in {"claude", "claude code", "new session", "new chat"}:
            raise DesktopError("暂时无法识别当前任务名称，请在 Ubuntu 展开当前项目侧栏后重试。")
    confirm_page_context(context)
    target = {"context": context, "mode": "Code", "title": title,
              "session_id": route, "native_session_id": route, "new_session": False,
              "ready": False, "reason": "正在读取当前会话", "capabilities": {"native_attachments": False}}
    return {"matched": True, "context": context, "project_title": project_title,
            "session_title": title, "target": target}


def project_headers():
    """Discover headers in one button query without reading any session names."""
    application = app()
    page, attrs = document(application)
    if not code_page(attrs):
        raise DesktopError("请先在 Ubuntu 的 Claude 客户端打开 Code 标签。")
    sidebars = [n for n in walk(page, prune_roles=("article",), prune_feeds=True)
                if "dframe-sidebar" in node_classes(n) and showing(n)]
    if len(sidebars) != 1:
        raise DesktopError("请在 Ubuntu 的 Code 窗口显示项目侧栏，再刷新项目列表。")
    atspi = initialize()
    groups = []
    controls = {}
    candidates = {}
    for header in buttons(sidebars[0]):
        if "group/label" not in node_classes(header) or not showing(header):
            continue
        group = header.parent
        while group is not None and group != sidebars[0] and "group/section" not in node_classes(group):
            group = group.parent
        if group is None or group == sidebars[0] or not showing(group):
            continue
        candidates.setdefault(group, []).append(header)
    for group, headers in candidates.items():
        if len(headers) != 1:
            raise DesktopError("Code 项目列表正在变化，请刷新后重试。")
        header = headers[0]
        header_state = header.getState()
        if not header_state.contains(atspi.STATE_EXPANDABLE):
            continue
        title = header.name.strip()
        if not title:
            continue
        project_id = project_selection_id("project", title)
        project = {"id": project_id, "title": title,
                   "expanded": header_state.contains(atspi.STATE_EXPANDED),
                   "selectable": header_state.contains(atspi.STATE_ENABLED) and header_state.contains(atspi.STATE_SENSITIVE),
                   "reason": "", "active": bool(node_attributes(header).get("current") == "page"
                                                   or header_state.contains(atspi.STATE_SELECTED)),
                   "sessions": [], "sessions_loaded": False}
        groups.append(project)
        controls.setdefault(project_id, []).append((project, header, group))
    counts = Counter(project["title"] for project in groups)
    for project in groups:
        if counts[project["title"]] > 1:
            project.update(selectable=False, reason="侧栏中有同名项目，请先在 Ubuntu 重命名后刷新。")
        elif not project["selectable"]:
            project["reason"] = "项目控件暂时不可用。"
    context = page_context(application, page, attrs)
    catalog_id = hashlib.sha256(json.dumps([context, groups], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    active_projects = [project for project in groups if project["active"]]
    result = {"mode": "Code", "scope": "projects", "selected_project_id": "", "context": context,
              "catalog_id": catalog_id, "projects": groups,
              "active_project_id": active_projects[0]["id"] if len(active_projects) == 1 else None,
              "active_session_id": None}
    confirm_page_context(context)
    return result, controls, attrs


def selected_project_catalog(snapshot, project_id):
    """Read native rows of exactly one chosen project, preserving their UI order."""
    catalog, controls, attrs = snapshot
    matches = controls.get(project_id, [])
    if len(matches) != 1 or not matches[0][0]["selectable"]:
        raise DesktopError("未找到唯一可选的项目，请刷新列表并检查同名项目。")
    project, header, group = matches[0]
    project = {**project, "sessions": [], "sessions_loaded": True}
    session_controls = []
    atspi = initialize()
    for node in buttons(group):
        classes = node_classes(node)
        if "hit-row-main" not in classes or "group/label" in classes or not showing(node):
            continue
        # Nested sections must not make another project's sessions part of this list.
        owner = node.parent
        while owner is not None and owner != group and "group/section" not in node_classes(owner):
            owner = owner.parent
        if owner != group:
            continue
        title = node.name.strip()
        state = node.getState()
        selected = node_attributes(node).get("current") == "page" or state.contains(atspi.STATE_SELECTED)
        session = {"id": project_selection_id("session", project_id, title), "title": title,
                   "active": bool(selected), "reason": "",
                   "selectable": bool(title and state.contains(atspi.STATE_ENABLED) and state.contains(atspi.STATE_SENSITIVE)),
                   "native_session_id": urlsplit(attrs["URI"]).path if selected and CODE_LOCAL_SESSION.fullmatch(
                       urlsplit(attrs["URI"]).path) else None}
        project["sessions"].append(session)
        session_controls.append((session, node))
    counts = Counter(session["title"] for session in project["sessions"])
    for session in project["sessions"]:
        if counts[session["title"]] > 1:
            session.update(selectable=False, reason="项目中有同名会话，请先在 Ubuntu 重命名后刷新。")
        elif not session["selectable"]:
            session["reason"] = "会话控件暂时不可用。"
    active = [session for session in project["sessions"] if session["active"]]
    if len(active) > 1:
        raise DesktopError("Code 当前会话标记不唯一，请在 Ubuntu 检查后刷新。")
    project["active"] = bool(active)
    projects = [project if item["id"] == project_id else dict(item) for item in catalog["projects"]]
    signature = hashlib.sha256(json.dumps([catalog["context"], project_id, project["expanded"], project["sessions"]],
                                         ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    confirm_page_context(catalog["context"])
    return {**catalog, "scope": "sessions", "selected_project_id": project_id, "projects": projects,
            "session_catalog_id": signature, "active_project_id": project_id if active else None,
            "active_session_id": active[0]["id"] if active else None}, session_controls


def project_catalog(project_id=""):
    snapshot = project_headers()
    return selected_project_catalog(snapshot, project_id) if project_id else (snapshot[0], snapshot[1])


def list_projects():
    return project_catalog()[0]


def read_project_catalog(request):
    project_id = request.get("project_id", "")
    if not isinstance(project_id, str) or not re.fullmatch(r"[0-9a-f]{64}", project_id):
        raise DesktopError("项目选择已失效，请刷新项目列表后重试。")
    snapshot = project_headers()
    for key in ("context", "catalog_id"):
        if request.get(key) and request[key] != snapshot[0][key]:
            raise DesktopError("Code 项目列表已经变化，请刷新后重新选择。")
    return selected_project_catalog(snapshot, project_id)[0]


def project_navigation_ready(expected_context):
    state = status()
    if state["mode"] != "Code":
        raise DesktopError("请先在 Ubuntu 的 Claude 客户端打开 Code 标签。")
    if state["context"] != expected_context:
        raise DesktopError("Ubuntu 当前会话已经变化，请刷新项目列表后重试。")
    if desktop_locked():
        raise DesktopError("Ubuntu 桌面已锁定，请先解锁。")
    if state["draft"] or state["attachments"] or state["dialogs"] or visible_menus():
        raise DesktopError("请先保留或处理 Ubuntu 当前草稿、附件及原生弹窗，再切换项目。")
    if not state["window_visible"]:
        raise DesktopError("请先打开 / 显示 Ubuntu Claude 客户端，再切换项目。")
    return state


def project_target_summary(state):
    result = {key: state.get(key) for key in (
        "context", "mode", "title", "ready", "reason", "busy", "window_visible", "new_session", "workspace", "session_id",
        "capabilities")}
    result["draft_chars"] = len(state.get("draft", ""))
    return result


def catalog_target(catalog, project_id, session_id):
    project = next(item for item in catalog["projects"] if item["id"] == project_id)
    matches = [item for item in project["sessions"] if item["id"] == session_id]
    if (len(matches) != 1 or not matches[0]["active"] or not matches[0]["selectable"]
            or not matches[0]["native_session_id"] or catalog["active_project_id"] != project_id
            or catalog["active_session_id"] != session_id):
        raise DesktopError("尚未确认所选项目的当前会话，请在 Ubuntu 检查。")
    return {"context": catalog["context"], "mode": "Code", "title": matches[0]["title"],
            "session_id": matches[0]["native_session_id"], "new_session": False,
            "ready": False, "reason": "正在读取当前会话", "capabilities": {"native_attachments": False}}


def navigate_project(request, open_recent=False):
    progress = {"session_action": False}
    try:
        return navigate_project_locked(request, open_recent, progress)
    except DesktopError as exc:
        if not progress["session_action"]:
            exc.not_submitted = True
        raise


def navigate_project_locked(request, open_recent, progress):
    expected = request.get("context", "")
    catalog_id = request.get("catalog_id", "")
    project_id = request.get("project_id", "")
    session_id = request.get("session_id") or ""
    session_catalog_id = request.get("session_catalog_id", "")
    if (not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                for value in (expected, catalog_id, project_id))
            or not isinstance(session_id, str) or session_id and not re.fullmatch(r"[0-9a-f]{64}", session_id)
            or session_id and (not isinstance(session_catalog_id, str)
                               or not re.fullmatch(r"[0-9a-f]{64}", session_catalog_id))):
        raise DesktopError("项目选择已失效，请刷新项目列表后重试。")
    with (ROOT / "desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DesktopError("客户端正在处理其他操作，请稍后再切换项目。") from None
        snapshot = project_headers()
        catalog, controls, _ = snapshot
        if catalog["context"] != expected or catalog["catalog_id"] != catalog_id:
            raise DesktopError("Code 项目列表已经变化，请刷新后重新选择。")
        projects = controls.get(project_id, [])
        if len(projects) != 1 or not projects[0][0]["selectable"]:
            raise DesktopError("未找到唯一可选的项目，请刷新列表并检查同名项目。")
        project, header, _ = projects[0]
        expanded = False
        if not project["expanded"]:
            if session_id:
                raise DesktopError("项目已折叠，请重新打开项目后选择会话。")
            checked = project_headers()
            if checked[0]["context"] != expected or checked[0]["catalog_id"] != catalog_id:
                raise DesktopError("Code 项目列表已经变化，请刷新后重新选择。")
            project, header, _ = checked[1][project_id][0]
            project_navigation_ready(expected)
            confirm_page_context(expected)
            header_state = header.getState()
            atspi = initialize()
            if (header.name.strip() != project["title"] or header_state.contains(atspi.STATE_EXPANDED)
                    or not all(header_state.contains(flag) for flag in (
                        atspi.STATE_EXPANDABLE, atspi.STATE_ENABLED, atspi.STATE_SENSITIVE))):
                raise DesktopError("所选项目控件已经变化，请刷新后重试。")
            if desktop_locked():
                raise DesktopError("Ubuntu 桌面已锁定，请先解锁。")
            if not showing(header) or not header.queryAction().doAction(0):
                raise DesktopError("Claude 未接受项目展开操作，请查看 Ubuntu 后刷新列表。")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                time.sleep(.15)
                snapshot = project_headers()
                if snapshot[0]["context"] != expected:
                    raise DesktopError("展开项目时 Ubuntu 会话发生变化，请刷新后重试。")
                projects = snapshot[1].get(project_id, [])
                if len(projects) != 1 or not projects[0][0]["selectable"]:
                    raise DesktopError("展开后的项目不再唯一可选，请刷新后重试。")
                if projects[0][0]["expanded"]:
                    expanded = True
                    break
            else:
                raise DesktopError("项目展开结果尚未确认，请查看 Ubuntu；不会自动再次点击。")
        catalog, session_controls = selected_project_catalog(snapshot, project_id)
        if session_id and catalog["session_catalog_id"] != session_catalog_id:
            raise DesktopError("所选项目的会话列表已经变化，请重新选择。")
        if open_recent:
            active = [(session, node) for session, node in session_controls if session["active"]]
            chosen = active if active else session_controls[:1]
            if not chosen:
                raise DesktopError("该项目暂无历史对话。")
            if len(chosen) != 1 or not chosen[0][0]["selectable"]:
                raise DesktopError("所选项目没有唯一可选的最近会话，请在 Ubuntu 检查；未切换到其他会话。")
            session_id = chosen[0][0]["id"]
        if session_id:
            sessions = [(session, node) for session, node in session_controls if session["id"] == session_id]
            if len(sessions) != 1 or not sessions[0][0]["selectable"]:
                raise DesktopError("未找到唯一可选的会话，请展开项目或在 Ubuntu 重命名同名会话后刷新。")
            session, control = sessions[0]
            if session["active"]:
                return {**catalog, "switched": False, "expanded": expanded,
                        "target": catalog_target(catalog, project_id, session_id)}
        else:
            # Compatibility: an empty manual selection only expands/reads, never opens a session.
            return {**catalog, "switched": False, "expanded": expanded}
        # Complete the expensive directory reads before the full draft/dialog guard.
        checked = project_headers()
        if checked[0]["context"] != expected or checked[0]["catalog_id"] != catalog["catalog_id"]:
            raise DesktopError("Code 项目列表已经变化，请刷新后重新选择。")
        latest, latest_controls = selected_project_catalog(checked, project_id)
        if latest["session_catalog_id"] != catalog["session_catalog_id"]:
            raise DesktopError("所选项目的会话列表已经变化，请重新选择。")
        sessions = [(item, node) for item, node in latest_controls if item["id"] == session_id]
        if len(sessions) != 1 or not sessions[0][0]["selectable"]:
            raise DesktopError("未找到唯一可选的会话，请刷新列表。")
        project_navigation_ready(expected)
        # Only the chosen group's small row query follows the guard, retaining
        # duplicate/title/context protection without another full-page traversal.
        final, final_controls = selected_project_catalog(checked, project_id)
        if final["session_catalog_id"] != latest["session_catalog_id"]:
            raise DesktopError("所选项目的会话列表已经变化，请重新选择。")
        sessions = [(item, node) for item, node in final_controls if item["id"] == session_id]
        if len(sessions) != 1 or not sessions[0][0]["selectable"]:
            raise DesktopError("未找到唯一可选的会话，请刷新列表。")
        control = sessions[0][1]
        if desktop_locked():
            raise DesktopError("Ubuntu 桌面已锁定，请先解锁。")
        if not showing(control):
            raise DesktopError("所选会话当前不可见，请刷新列表。")
        progress["session_action"] = True
        if not control.queryAction().doAction(0):
            raise DesktopError("Claude 未接受项目导航操作，请查看 Ubuntu 后刷新列表。")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(.15)
            try:
                updated, _ = project_catalog(project_id)
            except DesktopError:
                continue
            if updated["active_project_id"] == project_id and updated["active_session_id"] == session_id:
                return {**updated, "switched": True, "expanded": expanded,
                        "target": catalog_target(updated, project_id, session_id)}
        raise DesktopError("项目切换结果尚未确认，请查看 Ubuntu 当前会话；不会自动再次点击。")


def switch_project(request):
    return navigate_project(request)


def open_project(request):
    return navigate_project(request, open_recent=True)


def reply_content(node, depth=0):
    """Read message body text, omitting interface controls and accessibility announcements."""
    if depth > 65:
        return ""
    try:
        role, name = node.getRoleName(), node.name
        attrs = node_attributes(node)
        classes = attrs.get("class", "").split()
        tag = attrs.get("tag", "")
        if "sr-only" in classes or "group/status" in classes or "epitaxy-question-card" in classes or role in {
                "tool bar", "status bar", "push button", "toggle button", "entry", "menu", "menu item"} or tag in {"button", "time"}:
            return ""
        children = [child for child in node if child]
        if tag == "br":
            return "\n"
        if role == "table row" or tag == "tr":
            return "\t".join(reply_content(c, depth + 1).strip() for c in children) + "\n"
        body = "".join(reply_content(c, depth + 1) for c in children) if children else (
            name if role in {"static", "text", "link", "image"} else "")
        if role == "list item" or tag == "li":
            return "• " + body.strip() + "\n"
        if tag in {"p", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"} or role == "paragraph":
            return body.rstrip("\n") + "\n\n" if body else ""
        return body
    except Exception:
        return ""


class ReplyReadLimit(DesktopError):
    pass


class UnloadedTurnAnchor(ReplyReadLimit):
    """The visible tail is readable, but an earlier human row is virtualized."""
    pass


REPLY_CONTAINER_LIMIT = 512
REPLY_ARTICLE_LIMIT = 64
REPLY_BODY_LIMIT = 12000
REPLY_BODY_SECONDS = 8
REPLY_LEAF_ROLES = {"static", "text", "link", "image", "icon", "separator"}
REPLY_BODY_ROLES = REPLY_LEAF_ROLES | {"paragraph", "heading", "table", "table row", "list", "list item"}
QUESTION_WAIT_REASON = "Claude 正在等待你选择，请到 Ubuntu 查看并操作。"


def question_card_nodes(page):
    """Find current native question cards without reading message body names."""
    try:
        # Native Collection queries skip the old feed bodies entirely. Questions
        # are independent panels in the feed, not assistant message rows.
        from gi.repository import Atspi
        atspi = initialize()
        collection = page.queryCollection()
        rule = collection.createMatchRule(
            Atspi.StateSet.new([]), Atspi.CollectionMatchType.ALL, [], Atspi.CollectionMatchType.ALL,
            [int(atspi.ROLE_PANEL)], Atspi.CollectionMatchType.ANY,
            [], Atspi.CollectionMatchType.ALL, False)
        nodes = collection.getMatches(rule, Atspi.CollectionSortOrder.REVERSE_CANONICAL, 96, True)
        found, deadline = [], time.monotonic() + 2
        for node in nodes:
            if time.monotonic() > deadline:
                raise ReplyReadLimit("当前选择卡片结构尚未确认。")
            if "epitaxy-question-card" in node_classes(node):
                try:
                    visible = showing(node)
                except Exception:
                    # Keep the recognized card for a fail-soft notice if its
                    # accessibility object is changing during this read.
                    visible = True
                if visible:
                    found.append(node)
        if len(nodes) >= 96 and not found:
            raise ReplyReadLimit("当前选择卡片结构尚未确认。")
        return found
    except Exception:
        # Older accessibility bridges and synthetic checks use the same bounded
        # reverse metadata reader. Stop at the latest message instead of walking
        # back through history merely to prove that no question is present.
        feeds, found = [], []
        def question_visible(node, attrs):
            if "epitaxy-question-card" not in attrs.get("class", "").split():
                return False
            try:
                return showing(node)
            except Exception:
                return True
        for node, role, attrs in reply_metadata_nodes(page, reverse=True, prune_feeds=True):
            if question_visible(node, attrs):
                found.append(node)
            if message_feed(node, role, attrs):
                feeds.append(node)
        if len(feeds) == 1:
            for node, role, attrs in reply_metadata_nodes(feeds[0], reverse=True):
                if question_visible(node, attrs):
                    found.append(node)
                    break
                if role == "article" or ("group/message-row" in attrs.get("class", "").split()
                                         and direct_reply_payload(node)):
                    break
        return found


def pending_question(page):
    """Read a visible AskUserQuestion card only; never select or submit answers.

    A recognized but unreadable card still reports waiting, so lost option
    details never erase the instruction to finish the choice on Ubuntu.
    """
    empty = {"prompt": "", "page": None, "total_pages": None, "options": [],
             "multiple": False, "actions": [], "read_status": "unavailable"}
    try:
        cards = question_card_nodes(page)
    except Exception:
        return None
    if not cards:
        return None
    if len(cards) != 1:
        return empty
    card = cards[0]
    remaining, deadline = [320], time.monotonic() + 4

    def snapshot(node, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 20 or time.monotonic() > deadline:
            raise ReplyReadLimit(QUESTION_WAIT_REASON)
        role, attrs = node.getRoleName(), node_attributes(node)
        classes = attrs.get("class", "").split()
        if "sr-only" in classes or role in {"status bar", "tool bar", "menu", "menu item"}:
            return None
        count = node.childCount
        if count > remaining[0]:
            raise ReplyReadLimit(QUESTION_WAIT_REASON)
        # Current choices are toggle buttons; previous builds may use radio or
        # check boxes. Read the native state, never infer approval from defaults.
        selected = None
        if role in {"toggle button", "radio button", "check box"}:
            state, atspi = node.getState(), initialize()
            selected = any(state.contains(getattr(atspi, name))
                           for name in ("STATE_PRESSED", "STATE_CHECKED", "STATE_SELECTED")
                           if hasattr(atspi, name))
        name = node.name if not count or node is card or role in {
            "toggle button", "radio button", "check box", "push button"} else ""
        if len(name) > 20000:
            raise ReplyReadLimit(QUESTION_WAIT_REASON)
        children = [value for index in range(count)
                    if (value := snapshot(node.getChildAtIndex(index), depth + 1)) is not None]
        return {"role": role, "attrs": attrs, "name": name, "selected": selected, "children": children}

    def flattened(node):
        yield node
        for child in node["children"]:
            yield from flattened(child)

    def plain(node):
        if node["children"]:
            return " ".join(filter(None, (plain(child) for child in node["children"]))).strip()
        return node["name"].strip() if node["role"] in {"static", "text", "link", "entry"} else ""

    try:
        tree = snapshot(card)
        prompt = tree["name"].strip()
        if not prompt:
            raise ReplyReadLimit(QUESTION_WAIT_REASON)
        all_nodes = list(flattened(tree))
        options = []
        for node in all_nodes:
            if "epitaxy-answer-option" not in node["attrs"].get("class", "").split():
                continue
            choices = [child for child in flattened(node) if child["role"] in {
                "toggle button", "radio button", "check box"}]
            if len(choices) != 1:
                raise ReplyReadLimit(QUESTION_WAIT_REASON)
            choice = choices[0]
            primary = [plain(child) for child in flattened(node)
                       if "text-primary" in child["attrs"].get("class", "").split()]
            secondary = [plain(child) for child in flattened(node)
                         if "text-secondary" in child["attrs"].get("class", "").split()]
            label = " ".join(dict.fromkeys(filter(None, primary))) or choice["name"].strip()
            description = "\n".join(dict.fromkeys(filter(None, secondary)))
            if not label or len(label) + len(description) > 20000:
                raise ReplyReadLimit(QUESTION_WAIT_REASON)
            options.append({"label": label, "description": description, "selected": choice["selected"]})
        if not options or len(options) > 30:
            raise ReplyReadLimit(QUESTION_WAIT_REASON)
        pages = [match for node in all_nodes if node["role"] in {"static", "text"}
                 if (match := re.fullmatch(r"\s*(\d+)\s+of\s+(\d+)\s*", node["name"]))]
        page, total = (int(pages[0][1]), int(pages[0][2])) if len(pages) == 1 else (None, None)
        if page is not None and not 1 <= page <= total <= 100:
            raise ReplyReadLimit(QUESTION_WAIT_REASON)
        actions = [node["name"] for node in all_nodes if node["role"] == "push button" and node["name"] in {
            "Skip", "Next", "Submit", "Submit answers", "Show previous question", "Show next question"}]
        if card.name.strip() != prompt or not showing(card):
            raise ReplyReadLimit(QUESTION_WAIT_REASON)
        return {"prompt": prompt, "page": page, "total_pages": total, "options": options,
                "multiple": any(node["role"] == "check box" for node in all_nodes)
                            or tree["attrs"].get("multiselectable") == "true",
                "actions": list(dict.fromkeys(actions)), "read_status": "ready"}
    except Exception:
        return empty


def question_reply_text(question):
    """Text mirrors the visible card so existing translation can include it."""
    if not question or question.get("read_status") != "ready":
        return QUESTION_WAIT_REASON
    page, total = question.get("page"), question.get("total_pages")
    heading = "等待你选择" + (f" · 第 {page}/{total} 题" if page and total else "")
    lines = [heading, question["prompt"]]
    for index, option in enumerate(question["options"], 1):
        marker = " [已选中]" if option.get("selected") is True else ""
        lines.append(f"{index}.{marker} {option['label']}")
        if option.get("description"):
            lines.append("   " + option["description"].replace("\n", "\n   "))
    lines.append("请到 Ubuntu 查看并选择。")
    return "\n\n".join(lines)


def message_feed(node, role=None, attrs=None):
    role = role if role is not None else node.getRoleName()
    attrs = attrs if attrs is not None else node_attributes(node)
    return ("feed" in attrs.get("xml-roles", "").split()
            or role in {"panel", "landmark"} and node.name == "Chat messages")


def reply_metadata_nodes(root, *, reverse=False, prune_feeds=False, limit=REPLY_CONTAINER_LIMIT, turn_boundaries=False):
    """Bounded structural reads; message articles and semantic bodies are never expanded."""
    remaining = [limit]

    def visit(node, depth):
        if node is None:
            return
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 32:
            raise ReplyReadLimit("当前消息列表结构较复杂，未读取历史正文；请稍后重试最后一条回复。")
        role, attrs = node.getRoleName(), node_attributes(node)
        yield node, role, attrs
        classes = attrs.get("class", "").split()
        user_bubble = "group/bubble" in classes or "bg-[var(--cds-bg-user-message)]" in classes
        if (role == "article" or "group/message-row" in classes or "epitaxy-question-card" in classes or turn_boundaries and user_bubble
                or role in REPLY_BODY_ROLES or role in {
                "push button", "toggle button", "entry", "menu", "menu item", "tool bar"}
                or "sr-only" in attrs.get("class", "").split()
                or prune_feeds and message_feed(node, role, attrs)):
            return
        count = node.childCount
        indices = range(count - 1, -1, -1) if reverse else range(count)
        for index in indices:
            child = node.getChildAtIndex(index)
            if child is not None:
                yield from visit(child, depth + 1)

    yield from visit(root, 0)


def assistant_article(article):
    """Inspect only shallow native speaker metadata, never a user's body heading."""
    remaining = [24]

    def identify(node, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 5:
            return None
        role, attrs = node.getRoleName(), node_attributes(node)
        classes = attrs.get("class", "").split()
        if "group/message-row" in classes:
            return True
        if role == "heading" and "sr-only" in classes:
            name = node.name
            if name in {"Claude responded", "Claude said"} or name.startswith(("Claude responded:", "Claude said:")):
                return True
            if name in {"You said", "You asked"} or name.startswith(("You said:", "You asked:")):
                return False
            return None
        if role in REPLY_BODY_ROLES or role in {"push button", "toggle button", "entry", "tool bar"}:
            return None
        for index in range(min(node.childCount, 24)):
            result = identify(node.getChildAtIndex(index), depth + 1)
            if result is not None:
                return result
        return None

    return identify(article, 0) is True


def latest_reply_content(article):
    """Extract one selected article and its code blocks in a single bounded pass."""
    remaining, deadline, code = [REPLY_BODY_LIMIT], time.monotonic() + REPLY_BODY_SECONDS, []

    def read(node, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 65 or time.monotonic() > deadline:
            raise ReplyReadLimit("最后一条回复内容较多，读取尚未完成；请稍后刷新，不会扫描历史回复。")
        role, attrs = node.getRoleName(), node_attributes(node)
        classes, tag = attrs.get("class", "").split(), attrs.get("tag", "")
        if "sr-only" in classes or "group/status" in classes or "epitaxy-question-card" in classes or role in {
                "tool bar", "status bar", "push button", "toggle button", "entry", "menu", "menu item"} or tag in {"button", "time"}:
            return ""
        if tag == "br":
            return "\n"
        count = node.childCount
        if count > remaining[0]:
            raise ReplyReadLimit("最后一条回复内容较多，读取尚未完成；请稍后刷新，不会扫描历史回复。")
        parts = [read(node.getChildAtIndex(index), depth + 1) for index in range(count)]
        body = "".join(parts) if parts else node.name if role in {"static", "text", "link", "image"} else ""
        if tag == "pre" and body.strip("\n"):
            code.append(body.strip("\n"))
        if role == "table row" or tag == "tr":
            return "\t".join(part.strip() for part in parts) + "\n"
        if role == "list item" or tag == "li":
            return "• " + body.strip() + "\n"
        if tag in {"p", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"} or role == "paragraph":
            return body.rstrip("\n") + "\n\n" if body else ""
        return body

    try:
        body = read(article).strip("\n")
    except ReplyReadLimit:
        raise
    except Exception:
        raise DesktopError("最后一条回复读取未完成，请稍后刷新。") from None
    return body, code


def direct_reply_payload(row):
    """Ignore Code's footer/status-only rows without reading any accessible body names."""
    remaining = [128]

    def content(node, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 12:
            raise ReplyReadLimit("最后一条回复结构尚未载入，请稍后刷新。")
        role, attrs = node.getRoleName(), node_attributes(node)
        classes, tag = attrs.get("class", "").split(), attrs.get("tag", "")
        if "sr-only" in classes or "group/status" in classes or "epitaxy-question-card" in classes or role in {
                "tool bar", "status bar", "push button", "toggle button", "entry", "menu", "menu item"} or tag in {"button", "time"}:
            return False
        if role in REPLY_BODY_ROLES or tag in {"p", "pre", "blockquote", "ul", "ol", "table"}:
            return True
        for index in range(node.childCount):
            if content(node.getChildAtIndex(index), depth + 1):
                return True
        return False

    return content(row, 0)


def latest_reply(page):
    empty = {"text": "", "reply_count": 0, "code_blocks": [], "scope": "latest", "read_status": "empty"}
    try:
        feeds = [node for node, role, attrs in reply_metadata_nodes(page, reverse=True, prune_feeds=True)
                 if message_feed(node, role, attrs)]
        if len(feeds) != 1:
            return {**empty, "read_status": "unavailable", "read_reason": "当前会话的消息列表尚未载入，请稍后刷新。"}
        found = 0
        for node, role, attrs in reply_metadata_nodes(feeds[0], reverse=True):
            direct_row = "group/message-row" in attrs.get("class", "").split()
            if role != "article" and not direct_row:
                continue
            found += 1
            if found > REPLY_ARTICLE_LIMIT:
                raise ReplyReadLimit("当前显示的消息中尚未找到最后一条 Claude 回复，请在 Ubuntu 打开最新对话后重试。")
            if not direct_row and not assistant_article(node):
                continue
            # Code can expose native message rows directly, with a separate empty
            # footer row after the actual reply. That footer is not a new message.
            if direct_row and not direct_reply_payload(node):
                continue
            body, code = latest_reply_content(node)
            return {**empty, "text": body, "code_blocks": code, "reply_count": 1,
                    "read_status": "ready" if body else "empty",
                    "read_reason": "" if body else "最后一条 Claude 回复暂时没有正文，请等待输出。"}
        return {**empty, "read_reason": "当前会话还没有 Claude 回复。"}
    except ReplyReadLimit as exc:
        return {**empty, "read_status": "unavailable", "read_reason": str(exc)}
    except Exception:
        return {**empty, "read_status": "unavailable", "read_reason": "最后一条回复读取未完成，请稍后刷新。"}


TURN_MESSAGE_LIMIT = 128
TURN_METADATA_LIMIT = 1500
TURN_TEXT_LIMIT = 120000
TURN_CACHE_LIMIT = 2_000_000
TURN_SECONDS = 24
TURN_LOCK_SECONDS = 8


def accessible_key(node):
    """Native D-Bus object identity, not an index or a guessed content match."""
    value = getattr(node, "path", None)
    if not isinstance(value, str) or not re.fullmatch(r"/org/a11y/atspi/accessible/[A-Za-z0-9_/-]+", value):
        raise ReplyReadLimit("客户端暂未提供稳定消息标记，请稍后刷新当前回复。")
    return value


def turn_article_speaker(article):
    """Classify native speaker metadata without traversing either speaker's body."""
    headings = []
    remaining = [32]

    def identify(node, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 5:
            return None
        role, attrs = node.getRoleName(), node_attributes(node)
        classes = attrs.get("class", "").split()
        if "group/bubble" in classes or "bg-[var(--cds-bg-user-message)]" in classes:
            return "user"
        if "group/message-row" in classes:
            return "assistant"
        if role == "heading" and "sr-only" in classes:
            headings.append(node)
            return None
        if role in REPLY_BODY_ROLES or role in {"push button", "toggle button", "entry", "tool bar", "status bar"}:
            return None
        for index in range(min(node.childCount, 32)):
            found = identify(node.getChildAtIndex(index), depth + 1)
            if found is not None:
                return found
        return None

    speaker = identify(article, 0)
    labels = [heading.name for heading in headings]
    if speaker is None:
        if any(label in {"You said", "You asked"} or label.startswith(("You said:", "You asked:")) for label in labels):
            speaker = "user"
        elif any(label in {"Claude responded", "Claude said"} or label.startswith(("Claude responded:", "Claude said:")) for label in labels):
            speaker = "assistant"
    label_hash = hashlib.sha256(json.dumps(labels, ensure_ascii=False).encode()).hexdigest()
    return speaker, label_hash


def current_turn_snapshot(page, context):
    """Read reverse structural metadata only, stopping at the newest native human message."""
    feeds = [node for node, role, attrs in reply_metadata_nodes(page, reverse=True, prune_feeds=True)
             if message_feed(node, role, attrs)]
    if len(feeds) != 1:
        raise ReplyReadLimit("当前会话的消息列表尚未载入，请稍后刷新。")
    feed = feeds[0]
    feed_key = accessible_key(feed)
    rows, anchor = [], None
    for node, role, attrs in reply_metadata_nodes(feed, reverse=True, limit=TURN_METADATA_LIMIT, turn_boundaries=True):
        classes = attrs.get("class", "").split()
        is_user = "group/bubble" in classes or "bg-[var(--cds-bg-user-message)]" in classes
        direct_row = "group/message-row" in classes
        label_hash = ""
        if role == "article":
            speaker, label_hash = turn_article_speaker(node)
            if speaker is None:
                raise ReplyReadLimit("当前消息的归属尚未确认，请稍后刷新本轮回复。")
            is_user = speaker == "user"
            direct_row = speaker == "assistant"
        if is_user:
            anchor = [accessible_key(node), label_hash]
            break
        if not direct_row:
            continue
        if role != "article" and not direct_reply_payload(node):
            continue
        key = accessible_key(node)
        signature = hashlib.sha256(json.dumps([key, role, node.childCount, attrs.get("id", ""), attrs.get("tag", "")]).encode()).hexdigest()
        rows.append({"id": key, "signature": signature, "node": node})
        if len(rows) > TURN_MESSAGE_LIMIT:
            raise ReplyReadLimit("本轮回复较多，尚未完整确认消息顺序；请稍后刷新，不会跳过中间内容。")
    if anchor is None:
        if rows:
            raise UnloadedTurnAnchor("较早的用户消息尚未载入。")
        return {"turn_id": "", "feed_id": feed_key, "rows": []}
    turn_id = hashlib.sha256(json.dumps([context, feed_key, anchor], ensure_ascii=False).encode()).hexdigest()
    rows.reverse()
    if len({row["id"] for row in rows}) != len(rows):
        raise ReplyReadLimit("当前消息标记不唯一，请稍后刷新本轮回复。")
    return {"turn_id": turn_id, "feed_id": feed_key, "rows": rows}


def turn_cache_directory():
    directory = ROOT / "reply-cursors"
    if ROOT.is_symlink() or directory.is_symlink():
        raise DesktopError("回复缓存目录不安全，未读取缓存。")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid():
        raise DesktopError("回复缓存目录不属于当前用户。")
    directory.chmod(0o700)
    return directory


def read_turn_cache(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > TURN_CACHE_LIMIT:
            raise DesktopError("回复缓存文件不安全，未读取缓存。")
        data = stream.read(TURN_CACHE_LIMIT + 1)
    try:
        value = json.loads(data)
        rows = value.get("rows", [])
        if (value.get("schema") != 1 or not isinstance(rows, list) or len(rows) > TURN_MESSAGE_LIMIT
                or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                       or not isinstance(row.get("signature"), str) or not isinstance(row.get("text"), str)
                       or not isinstance(row.get("code_blocks"), list)
                       or any(not isinstance(block, str) for block in row["code_blocks"]) for row in rows)
                or sum(len(row["text"]) + sum(map(len, row["code_blocks"])) for row in rows) > 2 * TURN_TEXT_LIMIT):
            return {}
        return value
    except (ValueError, TypeError, AttributeError):
        return {}


def write_turn_cache(directory, path, value):
    raw = json.dumps(value, ensure_ascii=False).encode()
    if len(raw) > TURN_CACHE_LIMIT:
        raise ReplyReadLimit("本轮回复缓存达到上限，未跳过中间内容；请稍后刷新。")
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix="capture-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # Keep only a small number of this helper's owned context snapshots, not a chat archive.
    owned = []
    for candidate in directory.glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{64}\.json", candidate.name) or candidate.is_symlink():
            continue
        info = candidate.stat()
        if info.st_uid == os.getuid() and stat.S_ISREG(info.st_mode):
            owned.append((info.st_mtime, candidate))
    for _stamp, candidate in sorted(owned, reverse=True)[16:]:
        if candidate != path:
            candidate.unlink(missing_ok=True)


def current_turn_reply(page, context):
    """Accumulate current-turn rows, re-reading only the mutable tail and new rows."""
    empty = {"text": "", "reply_count": 0, "message_count": 0, "code_blocks": [], "scope": "current_turn",
             "turn_id": "", "read_status": "empty", "partial": False}
    try:
        if not isinstance(context, str) or not re.fullmatch(r"[0-9a-f]{64}", context):
            raise DesktopError("当前会话身份尚未确认，未读取回复缓存。")
        directory = turn_cache_directory()
        cache_path, lock_path = directory / (context + ".json"), directory / (context + ".lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "a+b") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise DesktopError("回复缓存锁不安全。")
            os.fchmod(lock.fileno(), 0o600)
            lock_deadline = time.monotonic() + TURN_LOCK_SECONDS
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= lock_deadline:
                        raise ReplyReadLimit("当前回复正在读取，请稍后刷新。") from None
                    time.sleep(.05)
            snapshot = current_turn_snapshot(page, context)
            saved = read_turn_cache(cache_path)
            if not snapshot["turn_id"]:
                if saved.get("context") == context and saved.get("rows"):
                    raise ReplyReadLimit("本轮消息起点暂未载入，请在 Ubuntu 显示当前对话后刷新。")
                return {**empty, "read_reason": "当前会话还没有本轮 Claude 回复。"}
            rows = snapshot["rows"]
            cached = saved.get("rows", []) if (saved.get("context") == context
                     and saved.get("turn_id") == snapshot["turn_id"]) else []
            aligned = bool(cached and len(cached) <= len(rows)
                           and [row["id"] for row in cached] == [row["id"] for row in rows[:len(cached)]])
            if cached and not aligned:
                raise ReplyReadLimit("本轮消息有折叠、重排或尚未载入的部分，未跳过中间内容；请在 Ubuntu 显示完整本轮对话后刷新。")
            reusable = bool(aligned
                            and all(old["signature"] == new["signature"] for old, new in zip(cached[:-1], rows)))
            start = max(0, len(cached) - 1) if reusable else 0
            captured = [dict(row) for row in cached[:start]] if reusable else []
            deadline = time.monotonic() + TURN_SECONDS
            for row in rows[start:]:
                if time.monotonic() > deadline and len(captured) > start:
                    break
                body, code = latest_reply_content(row["node"])
                captured.append({"id": row["id"], "signature": row["signature"], "text": body, "code_blocks": code})
                if sum(len(item["text"]) for item in captured) > TURN_TEXT_LIMIT:
                    raise ReplyReadLimit("本轮回复文字达到读取上限，未跳过中间内容。")
            after = current_turn_snapshot(page, context)
            # Persist only a verified ordered prefix when a large turn needs more than one poll.
            # The next poll refreshes this prefix's mutable tail, then continues immediately after it.
            ids = [row["id"] for row in rows[:len(captured)]]
            later_ids = [row["id"] for row in after["rows"]]
            if after["turn_id"] != snapshot["turn_id"] or later_ids[:len(ids)] != ids:
                raise ReplyReadLimit("本轮消息已变化，请刷新重新确认顺序。")
            if any(old["signature"] != new["signature"] for old, new in zip(rows[:len(captured) - 1], after["rows"])):
                raise ReplyReadLimit("本轮较早消息的结构正在变化，请刷新重新确认内容。")
            partial = (len(later_ids) != len(ids)
                       or bool(ids and rows[len(ids) - 1]["signature"] != after["rows"][len(ids) - 1]["signature"]))
            confirm_page_context(context)
            write_turn_cache(directory, cache_path, {"schema": 1, "context": context, "turn_id": snapshot["turn_id"],
                                                     "rows": captured, "updated_at": time.time()})
            body = "\n\n".join(row["text"] for row in captured if row["text"])
            blocks = [block for row in captured for block in row["code_blocks"]]
            count = sum(bool(row["text"]) for row in captured)
            return {**empty, "text": body, "code_blocks": blocks, "reply_count": count, "message_count": count,
                    "turn_id": snapshot["turn_id"], "read_status": "ready" if body else "empty", "partial": partial,
                    "read_reason": "已读取本轮连续回复，下一次继续补齐。" if partial else "" if body else "等待 Claude 输出本轮回复。"}
    except UnloadedTurnAnchor:
        # Do not turn the mounted tail into a fabricated complete turn, or
        # mix it with the old cursor. Only return the one latest native reply.
        reply = latest_reply(page)
        if reply.get("read_status") == "ready":
            confirm_page_context(context)
            return {**reply, "message_count": reply["reply_count"], "turn_id": "", "partial": False,
                    "read_reason": "较早的用户消息未载入，已显示当前最后一条 Claude 回复。"}
        return {**reply, "message_count": 0, "turn_id": "", "partial": False}
    except (ReplyReadLimit, DesktopError) as exc:
        return {**empty, "read_status": "unavailable", "read_reason": str(exc)}
    except Exception:
        return {**empty, "read_status": "unavailable", "read_reason": "本轮回复读取未完成，请稍后刷新。"}


def reply_in_progress(page):
    """Read only controls/announcements outside the history feed, with a fixed budget."""
    try:
        for node, role, attrs in reply_metadata_nodes(page, reverse=True, prune_feeds=True):
            name = node.name if role == "push button" else None
            if role == "push button" and name in STOP_BUTTONS:
                return True
            if attrs.get("container-live") and (name if name is not None else node.name) == "Claude is responding":
                return True
    except Exception:
        pass
    return False


def status():
    application = app()
    page, attrs = document(application)
    is_code = code_page(attrs)
    mode = "Code" if is_code else "Chat"
    entry = composer(page, attrs)
    controls = code_dock(entry) if is_code else page
    names = [n.name for n in buttons(controls) if showing(n)]
    process_id = application.get_process_id()
    route = urlsplit(attrs["URI"]).path
    new_session = is_code and route in CODE_NEW_PATHS
    new_target = new_code_target(attrs, controls) if new_session else None
    setup_visible = new_session and any(name.startswith("Environment: ") for name in names)
    context = conversation_context(process_id, attrs["URI"], new_target)
    question = pending_question(page) if is_code else None
    draft = text(entry).strip()
    attachments = list(native_attachment_controls(controls, is_code))
    attachment_pending = is_code and sum(
        1 for n in walk(controls) if "group/tile" in node_classes(n) and showing(n)) > len(attachments)
    busy = any(n in STOP_BUTTONS for n in names)
    dialogs = []
    for node in role_nodes(application, ("dialog", "alert dialog")):
        try:
            if node.getRoleName() in {"dialog", "alert dialog"}:
                dialogs.append(node.name)
        except Exception:
            pass
    # A file chooser lives in a different application and must never receive keystrokes.
    atspi = initialize()
    for application in atspi.Registry.getDesktop(0):
        if application.name.startswith("xdg-desktop-portal"):
            for node in role_nodes(application, ("dialog", "file chooser")):
                try:
                    if node.getRoleName() in {"dialog", "file chooser"} and node.name in {"Open Files", "Open File"}:
                        dialogs.append("Open Files")
                except Exception:
                    pass
    state = entry.getState()
    enabled = state.contains(atspi.STATE_ENABLED) and state.contains(atspi.STATE_EDITABLE)
    approval = is_code and code_approval_pending(controls, entry)
    session_ready = not is_code or bool(CODE_LOCAL_SESSION.fullmatch(route)) or new_target is not None
    locked = desktop_locked()
    window_error = ""
    try:
        target_window = input_window()
        visible = True
    except DesktopError as exc:
        visible = False
        window_error = str(exc)
    running_input = bool(is_code and CODE_LOCAL_SESSION.fullmatch(route) and enabled
                         and entry.getRoleName() == "entry" and entry.name == "Prompt"
                         and {"tiptap", "ProseMirror"}.issubset(node_classes(entry)) and showing(entry))
    common_ready = bool(session_ready and enabled and not draft and not attachments and not attachment_pending
                        and not question and not dialogs and not approval and not locked)
    input_ready = common_ready and not busy
    send_input_ready = common_ready and (not busy or running_input)
    reason = "可以接收指令" if input_ready and visible else (
        "Ubuntu 桌面已锁定，请先解锁" if locked else
        QUESTION_WAIT_REASON if question is not None else
        "请在 Ubuntu 的 Code 新会话中选择 This computer 和项目目录" if not session_ready else
        "Claude Code 要求重新登录。请在 Ubuntu 点 Sign in again 完成验证。" if is_code and "Sign in again" in names else
        "Claude Code 正在等待你处理提问或权限请求" if approval else
        "请查看客户端当前草稿、任务或弹窗" if not input_ready else window_error)
    send_ready = bool(send_input_ready and visible)
    send_reason = ("可以追加消息；当前任务会继续运行。" if send_ready and busy else
                   "可以接收指令" if send_ready else
                   "当前客户端运行时追加消息入口尚未就绪，请到 Ubuntu 查看。" if busy and not running_input else reason)
    return {"context": context, "title": attrs.get("Title", "Claude"), "mode": mode, "draft": draft,
            "process_id": process_id, "new_session": new_session,
            "setup_visible": setup_visible,
            "workspace": new_target["workspace"] if new_target else None,
            "session_id": route if is_code and CODE_LOCAL_SESSION.fullmatch(route) else None,
            "attachments": attachments, "busy": busy, "dialogs": dialogs,
            "attachment_pending": bool(attachment_pending),
            "locked": bool(locked),
            "approval": approval,
            "awaiting_input": question is not None, "question": question,
            "input_ready": input_ready, "window_visible": visible, "window_error": window_error,
            "send_ready": send_ready, "send_input_ready": bool(send_input_ready), "send_reason": send_reason,
            "send_mode": "running_insert" if send_ready and busy else "idle" if send_ready else "unavailable",
            "running_input_capable": running_input,
            "capabilities": {**native_attachment_capabilities(process_id, is_code, entry, controls, visible),
                             "running_attachments": False},
            "ready": input_ready and visible, "reason": reason,
            "model": next((n[7:] for n in names if n.startswith("Model: ")), "客户端当前设置")}


def check_ready(request):
    state = status()
    for_send = request.get("for_send") is True
    def eligible(value):
        return (value.get("send_input_ready", value.get("send_ready", value.get("input_ready", value["ready"])))
                if for_send else value.get("input_ready", value["ready"]))
    if request.get("context") and request["context"] != state["context"]:
        raise DesktopError("Ubuntu 已切换对话或重启客户端，已停止发送。请重新检查目标。")
    if not eligible(state):
        raise DesktopError("Claude 客户端尚未就绪：" + state.get("send_reason" if for_send else "reason", "请保留或处理现有草稿/附件，关闭弹窗，或等待当前回复完成。"))
    try:
        target_window = input_window()
    except WindowUnavailable:
        original_context = state["context"]
        start(request)
        state = status()
        if state["context"] != original_context:
            raise DesktopError("客户端恢复后对话发生变化，尚未发送。请检查 Ubuntu 的目标对话后再发送。")
        if not eligible(state):
            raise DesktopError("客户端窗口已恢复，但尚未就绪：" + state["reason"])
        target_window = input_window()
    return {"ready": True, "context": state["context"], "title": state["title"],
            "input_transport": "wayland" if isinstance(target_window, dict) else "x11",
            "mode": state.get("mode", "Chat"),
            "capabilities": state.get("capabilities", {"native_attachments": False}),
            "busy": bool(state.get("busy")), "send_ready": True if for_send else bool(state.get("send_ready", state["ready"])),
            "send_reason": state.get("send_reason", state.get("reason", "")), "send_mode": state.get("send_mode", "idle"),
            "allow_running": bool(for_send and state.get("running_input_capable")),
            **{key: state.get(key) for key in ("process_id", "new_session", "workspace", "session_id")}}


def code_submission_confirmed(initial, current, value):
    if not initial.get("new_session"):
        if current["context"] != initial["context"]:
            raise DesktopError("提交后客户端已切换对话，结果未确认，请查看 Ubuntu。")
        return not current["draft"] and not current["attachments"]
    if current.get("mode") != "Code" or current.get("process_id") != initial["process_id"]:
        raise DesktopError("提交后客户端或会话类型发生变化，结果未确认，请查看 Ubuntu。")
    if current.get("new_session"):
        if current.get("setup_visible") is False and not current["draft"] and not current["attachments"]:
            return False
        if current["context"] != initial["context"]:
            raise DesktopError("提交后新会话的环境或目录发生变化，结果未确认，请查看 Ubuntu。")
        return False
    if not current.get("session_id"):
        raise DesktopError("提交后未识别到本地 Code 会话，结果未确认，请查看 Ubuntu。")
    if current["draft"] or current["attachments"]:
        return False
    page, attrs = document()
    context = hashlib.sha256((str(initial["process_id"]) + "|" + attrs["URI"]).encode()).hexdigest()
    if context != current["context"]:
        raise DesktopError("读取回执时客户端已切换对话，结果未确认，请查看 Ubuntu。")
    prompts = []
    for article in walk(code_panel(page)):
        if article.getRoleName() == "article" and any(
                n.getRoleName() == "heading" and (n.name == "You said" or n.name.startswith("You said:"))
                for n in walk(article)):
            bubbles = [n for n in walk(article) if "bg-[var(--cds-bg-user-message)]" in node_classes(n)]
            if len(bubbles) == 1:
                prompts.append("".join(reply_content(child) for child in bubbles[0] if child and not (
                    child.getRoleName() == "static" and child.name in {"Message collapsed", "Message expanded"})))
            else:
                prompts.append(reply_content(article))
    if not prompts or len(prompts) == 1 and not prompts[0].strip():
        return False
    if len(prompts) != 1 or " ".join(prompts[0].split()) != " ".join(value.split()):
        raise DesktopError("新 Code 会话中未核对到本次首条消息，请查看 Ubuntu，不会自动重发。")
    return True


def running_user_snapshot(page, *, read_text=False, after_tail=None):
    """Read one human body; a mounted tail can witness a virtualized old user."""
    feeds = [node for node, role, attrs in reply_metadata_nodes(page, reverse=True, prune_feeds=True)
             if message_feed(node, role, attrs)]
    if len(feeds) != 1:
        raise DesktopError("追加消息的当前消息列表尚未就绪，请到 Ubuntu 查看；不会自动重发。")
    feed, selected, tail = feeds[0], None, None
    try:
        if after_tail is not None or not read_text:
            raise LookupError("A submission boundary requires one ordered metadata scan")
        from gi.repository import Atspi
        atspi, collection = initialize(), feed.queryCollection()
        rule = collection.createMatchRule(
            Atspi.StateSet.new([]), Atspi.CollectionMatchType.ALL, [], Atspi.CollectionMatchType.ALL,
            [int(atspi.ROLE_ARTICLE)], Atspi.CollectionMatchType.ANY,
            [], Atspi.CollectionMatchType.ALL, False)
        for article in collection.getMatches(rule, Atspi.CollectionSortOrder.REVERSE_CANONICAL, 32, True):
            if turn_article_speaker(article)[0] == "user":
                selected = article
                break
    except Exception:
        pass
    if selected is None or after_tail is not None:
        found_tail = False
        for node, role, attrs in reply_metadata_nodes(feed, reverse=True, limit=768, turn_boundaries=True):
            classes = attrs.get("class", "").split()
            speaker = turn_article_speaker(node)[0] if role == "article" else None
            is_user = ("group/bubble" in classes or "bg-[var(--cds-bg-user-message)]" in classes
                       or speaker == "user")
            is_assistant = "group/message-row" in classes or speaker == "assistant"
            if after_tail is not None:
                if is_assistant and accessible_key(node) == after_tail:
                    found_tail = True
                    break
                if is_user:
                    # Keep the first human AFTER the original tail, even if
                    # newer human turns have since appeared in the same feed.
                    selected = node
            else:
                if tail is None and is_assistant:
                    tail = node
                    if not read_text:
                        return {"feed_id": accessible_key(feed), "anchor_kind": "tail_row", "tail_id": accessible_key(tail)}
                if is_user:
                    selected = node
                    break
        if after_tail is not None and (not found_tail or selected is None):
            raise DesktopError("追加消息尚未出现在原消息边界之后，请到 Ubuntu 查看；不会自动重发。")
    if selected is None:
        if not read_text and tail is not None:
            return {"feed_id": accessible_key(feed), "anchor_kind": "tail_row", "tail_id": accessible_key(tail)}
        raise DesktopError("尚未确认追加消息的原始用户消息标记，请到 Ubuntu 查看；不会自动重发。")
    result = {"feed_id": accessible_key(feed), "message_id": accessible_key(selected)}
    if not read_text:
        return result
    selected_classes = node_classes(selected)
    if "group/bubble" in selected_classes or "bg-[var(--cds-bg-user-message)]" in selected_classes:
        bubble = selected
    else:
        bubbles, budget = [], [96]
        def find_bubbles(node, depth=0):
            budget[0] -= 1
            if budget[0] < 0 or depth > 10:
                raise ReplyReadLimit("追加消息的正文结构尚未确认。")
            role, attrs = node.getRoleName(), node_attributes(node)
            classes = attrs.get("class", "").split()
            if "group/bubble" in classes or "bg-[var(--cds-bg-user-message)]" in classes:
                bubbles.append(node)
                return
            if role in REPLY_BODY_ROLES or role in {"push button", "entry", "tool bar"} or "sr-only" in classes:
                return
            for index in range(node.childCount):
                find_bubbles(node.getChildAtIndex(index), depth + 1)
        find_bubbles(selected)
        if len(bubbles) != 1:
            raise DesktopError("追加消息的正文归属尚未确认，请到 Ubuntu 查看；不会自动重发。")
        bubble = bubbles[0]
    remaining, deadline = [2000], time.monotonic() + 4
    def read(node, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 48 or time.monotonic() > deadline:
            raise ReplyReadLimit("追加消息的正文读取尚未完成。")
        role, attrs = node.getRoleName(), node_attributes(node)
        classes, tag = attrs.get("class", "").split(), attrs.get("tag", "")
        if "sr-only" in classes or role in {"tool bar", "status bar", "push button", "toggle button", "entry", "menu", "menu item"}:
            return ""
        if tag == "br":
            return "\n"
        count = node.childCount
        if count > remaining[0]:
            raise ReplyReadLimit("追加消息的正文读取尚未完成。")
        if count:
            body = "".join(read(node.getChildAtIndex(index), depth + 1) for index in range(count))
        else:
            body = node.name if role in {"static", "text", "link"} else ""
            if role == "static" and body in {"Message collapsed", "Message expanded"}:
                return ""
        if role == "paragraph" or tag in {"p", "pre", "blockquote", "li"}:
            return body + "\n"
        return body
    result["text"] = read(bubble).strip()
    return result


def normalized_message_hash(value):
    return hashlib.sha256(" ".join(value.split()).encode("utf-8")).hexdigest()


def running_submission_confirmed(initial, current, expected_hash, baseline):
    """A pre-existing busy state or empty composer is never proof of acceptance."""
    if (current.get("context") != initial["context"] or current.get("mode") != "Code"
            or current.get("process_id") != initial.get("process_id")):
        raise DesktopError("追加消息后客户端会话已经变化，结果未确认，请到 Ubuntu 查看。")
    if (current.get("draft") or current.get("attachments") or current.get("locked", False)
            or not current.get("window_visible", True)):
        return False
    application = app()
    page, attrs = document(application)
    if page_context(application, page, attrs) != initial["context"]:
        raise DesktopError("核对追加消息时客户端已切换会话，结果未确认。")
    after = (running_user_snapshot(code_panel(page), read_text=True, after_tail=baseline["tail_id"])
             if baseline.get("anchor_kind") == "tail_row" else running_user_snapshot(code_panel(page), read_text=True))
    confirm_page_context(initial["context"])
    if (after["feed_id"] != baseline["feed_id"] or after["message_id"] == baseline.get("message_id")
            or normalized_message_hash(after["text"]) != expected_hash):
        return False
    return {"kind": "new_user_message", "message_id_hash": hashlib.sha256(after["message_id"].encode()).hexdigest()}


def running_enter_ready(initial, value):
    """Ordinary Return is allowed only in the exact focused running Prompt."""
    if initial.get("allow_running") is not True or initial.get("mode") != "Code" or initial.get("new_session"):
        raise DesktopError("当前会话不支持运行时追加消息，已保留草稿。")
    # Full status/approval checks may take seconds. Perform them BEFORE the
    # final focused Prompt read, so a user moving focus during that scan cannot
    # leave Return targeting another control in the same Claude window.
    current = status()
    require_attachment_draft(current, initial["context"], [], value.strip(), allow_running=True)
    if current.get("process_id") != initial.get("process_id"):
        raise DesktopError("追加消息的客户端已经变化，已保留草稿。")
    wid = input_window()
    application = app()
    page, attrs = document(application)
    if page_context(application, page, attrs) != initial["context"]:
        raise DesktopError("追加消息目标已经变化，已保留草稿。")
    entry, atspi = composer(page, attrs), initialize()
    if visible_menus():
        raise DesktopError("Ubuntu 当前有原生菜单打开，已保留追加消息草稿。")
    state = entry.getState()
    if (entry.getRoleName() != "entry" or entry.name != "Prompt"
            or not {"tiptap", "ProseMirror"}.issubset(node_classes(entry)) or not showing(entry)
            or not all(state.contains(flag) for flag in (atspi.STATE_FOCUSED, atspi.STATE_ENABLED, atspi.STATE_EDITABLE))
            or text(entry).strip() != value.strip()):
        raise DesktopError("追加消息输入框未保持精确焦点或草稿已变化，请到 Ubuntu 查看。")
    if isinstance(wid, dict):
        if wid["context"] != initial["context"] or wid["process_id"] != initial.get("process_id"):
            raise DesktopError("追加消息的原生窗口已经变化，已保留草稿。")
        return
    from Xlib import X, display
    connection = display.Display(environment()["DISPLAY"])
    try:
        root = connection.screen().root
        active = root.get_full_property(connection.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
        held = X.ShiftMask | X.ControlMask | X.Mod1Mask | X.Mod4Mask | X.Mod5Mask | X.Button1Mask | X.Button2Mask | X.Button3Mask
        if active is None or len(active.value) != 1 or int(active.value[0]) != wid or root.query_pointer().mask & held:
            raise DesktopError("Ubuntu 焦点或按键状态已经变化，已保留追加消息草稿。")
    finally:
        connection.close()


def reconcile_running_delivery(ledger, record):
    """Only new running receipts may observe a later native acceptance; no input."""
    pending = record.get("running_submission")
    valid_path = lambda value: isinstance(value, str) and bool(re.fullmatch(r"/org/a11y/atspi/accessible/[A-Za-z0-9_/-]+", value))
    if (record.get("state") != "submitting" or record.get("auto_enter") is not True
            or not isinstance(pending, dict) or type(pending.get("schema")) is not int or pending["schema"] not in {1, 2}
            or not isinstance(record.get("context"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["context"])
            or not isinstance(pending.get("process_id"), int) or isinstance(pending["process_id"], bool) or pending["process_id"] <= 0
            or not valid_path(pending.get("feed_id"))
            or (pending["schema"] == 1 and (not valid_path(pending.get("message_id")) or "anchor_kind" in pending or "tail_id" in pending))
            or (pending["schema"] == 2 and (pending.get("anchor_kind") != "tail_row" or not valid_path(pending.get("tail_id")) or "message_id" in pending))
            or not isinstance(pending.get("expected_text_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", pending["expected_text_sha256"])):
        return record
    try:
        # Serialize only this receipt while observing UI. Holding desktop.lock
        # through a read-only AT-SPI scan starves new sends behind old receipts.
        receipt_path = ledger.with_suffix(".reconcile.lock")
        descriptor = os.open(receipt_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "a+b") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return record
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return record
            def unchanged(fresh):
                return (isinstance(fresh, dict) and fresh.get("request_id") == record.get("request_id")
                    and fresh.get("fingerprint") == record.get("fingerprint")
                    and fresh.get("context") == record.get("context") and fresh.get("auto_enter") is True
                    and fresh.get("running_submission") == pending and fresh.get("state") == "submitting")
            desktop_fd = os.open(ROOT / "desktop.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(desktop_fd, "a+b") as desktop_lock:
                info = os.fstat(desktop_lock.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    return record
                try:
                    fcntl.flock(desktop_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return record
                fresh = json.loads(ledger.read_text(encoding="utf-8"))
                if not unchanged(fresh):
                    return record
                # No input is performed below. Release the global input lock
                # before status/body reads; final context and ledger checks
                # independently guard against a concurrent new send/navigation.
            initial = {"context": record["context"], "mode": "Code", "process_id": pending["process_id"]}
            proof = running_submission_confirmed(initial, status(), pending["expected_text_sha256"], pending)
            if proof:
                import remote
                fresh = json.loads(ledger.read_text(encoding="utf-8"))
                if not unchanged(fresh):
                    return record
                fresh.update(state="submitted", submission_evidence=proof, reconciled_at=time.time())
                remote.atomic(ledger, fresh)
                return fresh
    except Exception:
        # A lock, changed session, disappearing AX node, or unreadable body is
        # unknown, never a reason to repeat the original submit action.
        pass
    return record


def paste(value, expected_context=None, *, allow_running=False):
    clipboard = str(TOOLS / "usr/bin/xclip")
    def read():
        try:
            result = subprocess.run([clipboard, "-selection", "clipboard", "-out"], env=environment(),
                capture_output=True, timeout=1)
            return result.stdout if not result.returncode and len(result.stdout) <= 1_000_000 else None
        except subprocess.TimeoutExpired:
            return None
    def write(raw):
        subprocess.run([clipboard, "-selection", "clipboard", "-in"], env=environment(), input=raw,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=True)
    previous = read()
    data = value.encode("utf-8")
    target = (focus_composer(expected_context, allow_running=True) if allow_running else focus_composer(expected_context))
    keyboard = NativeKeyboard() if isinstance(target, dict) else None
    try:
        write(data)
        for _ in range(10):
            if read() == data:
                break
            time.sleep(.05)
        time.sleep(.15)
        if keyboard is not None:
            require_native_focus(target, expected_context, "", allow_running=allow_running)
            keyboard.key("Control_L+v")
        else:
            xcommand("key", "--clearmodifiers", "ctrl+v")
        for _ in range(60):
            pump_events()
            if text(composer()).strip() == value.strip():
                return
            time.sleep(.1)
        raise DesktopError("客户端文字载入未确认，消息尚未提交。请查看 Ubuntu 草稿，避免重复载入。")
    finally:
        try:
            if read() == data:
                write(previous or b"")
        finally:
            if keyboard is not None:
                keyboard.close()


def ensure_workspace(request):
    import remote
    name = remote.session_name(request)
    cwd = Path(request.get("cwd", ""))
    if not cwd.is_absolute() or not cwd.is_relative_to(Path.home()) or cwd == Path.home():
        raise DesktopError("附件目录须为 Ubuntu 用户目录下的独立绝对路径。")
    cwd.mkdir(parents=True, exist_ok=True)
    if not cwd.resolve().is_relative_to(Path.home().resolve()):
        raise DesktopError("附件目录不能指向用户目录之外。")
    meta = remote.ROOT / "sessions" / (name + ".json")
    if meta.exists():
        saved = json.loads(meta.read_text())
        if Path(saved["cwd"]).resolve() != cwd.resolve():
            raise DesktopError("该附件存储已绑定其他目录，请更换配置中的存储标识。")
    else:
        remote.atomic(meta, {"cwd": str(cwd.resolve()), "transport": "desktop"})


DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".csv", ".txt", ".html", ".htm", ".odt", ".rtf", ".epub", ".json", ".xlsx",
    ".md", ".markdown", ".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".xml", ".yaml", ".yml", ".toml",
    ".ini", ".log", ".sh", ".ps1", ".sql", ".c", ".cpp", ".h", ".hpp", ".cs", ".java", ".go", ".rs", ".rb", ".vue", ".svelte"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def attachment_paths(request):
    """Select real GUI uploads only from the previously verified upload manifest."""
    import remote
    batch = request.get("attachment_batch")
    if not batch:
        return []
    name = remote.session_name(request)
    remote.verify_attachments(name, batch)
    _, manifest = remote.load_attachment_batch(name, batch)
    files = manifest["files"]
    selected = []
    for index, original in enumerate((f for f in files if f["role"] == "original"), 1):
        derived = [f for f in files if f.get("parent_id") == original["id"]]
        suffix = Path(original["name"]).suffix.lower()
        choices = [original]
        if original["kind"] == "video":
            choices = [f for f in derived if f["role"] in {"preview", "metadata"}]
            if len(choices) != 2:
                raise DesktopError("视频抽帧失败，尚未发送。请检查本机 FFmpeg：" + original["name"])
        elif original["kind"] == "image":
            from PIL import Image
            with Image.open(original["path"]) as image:
                large = max(image.size) > 8000
            if suffix not in IMAGE_EXTENSIONS or large:
                choices = [f for f in derived if f["role"] == "preview"]
                if not choices:
                    raise DesktopError("图片格式或尺寸不适合客户端，且预览不可用：" + original["name"])
        elif suffix not in DOCUMENT_EXTENSIONS:
            raise DesktopError("Claude 桌面聊天当前不支持直接读取此格式：" + original["name"] + "。原件已保存在 Ubuntu，尚未提交消息。")
        for entry in choices:
            if entry["size"] > 500_000_000:
                raise DesktopError("客户端单个附件超过 500 MB：" + original["name"])
            src = remote.guarded_attachment_path(name, entry["path"])
            folder = src.parent / "desktop"
            folder.mkdir(exist_ok=True, mode=0o700)
            if folder.is_symlink() or not folder.resolve().is_relative_to(src.parent.resolve()):
                raise DesktopError("客户端附件目录已改变。")
            title = original["name"] if entry["role"] == "original" else Path(original["name"]).stem + ("-timeline.jpg" if entry["role"] == "preview" else "-metadata.json")
            title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", title)
            if len(title) > 120:
                title = title[:95] + Path(title).suffix
            destination = folder / (f"{index:02d}-{batch[:8]}-" + title)
            try:
                os.link(src, destination)
            except FileExistsError:
                if destination.is_symlink() or remote.file_hash(destination) != entry["sha256"]:
                    raise DesktopError("客户端附件目标已存在不同内容。")
            selected.append(str(destination))
    if len(selected) > 20:
        raise DesktopError("Claude 客户端单条消息最多 20 个文件（每个视频会使用抽帧图和元数据两个文件）。请分批发送。")
    return selected


def load_native_attachment_draft(value, paths, initial):
    allow_running = initial.get("allow_running") is True and not paths
    def require(current, draft, partial=False):
        return require_attachment_draft(current, initial["context"], paths, draft, partial,
                                        allow_running=allow_running)
    if paths:
        drop_files(paths, initial["context"])
        for _ in range(200):
            current = status()
            matched = require(current, "", partial=True)
            if len(matched) == len(paths) and not current.get("attachment_pending"):
                break
            time.sleep(.15)
        else:
            raise DesktopError("客户端附件载入未完整确认，已保留草稿，不会提交。")
        time.sleep(.8)
    require(status(), "")
    if allow_running:
        paste(value, initial["context"], allow_running=True)
    else:
        paste(value, initial["context"])
    current = status()
    require(current, value.strip())
    return current


def delivery_status(request):
    """Read a receipt; only new running witnesses may reconcile via read-only AX."""
    rid = request.get("request_id")
    fingerprint = request.get("fingerprint")
    if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{32}", rid):
        raise DesktopError("发送记录 ID 无效。")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise DesktopError("发送记录校验值无效。")
    missing = {"found": False, "matched": False, "request_id": rid, "completed": False}
    directory = ROOT / "desktop-deliveries"
    ledger = directory / (rid + ".json")
    if directory.is_symlink() or ledger.is_symlink():
        return missing
    try:
        record = json.loads(ledger.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return missing
    except (OSError, ValueError):
        raise DesktopError("发送记录暂时无法读取，结果仍未确认。") from None
    if (not isinstance(record, dict) or record.get("request_id") != rid
            or not isinstance(record.get("fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["fingerprint"])
            or not hmac.compare_digest(record["fingerprint"], fingerprint)):
        return missing
    record = reconcile_running_delivery(ledger, record)
    state = record.get("state")
    if not isinstance(state, str) or state not in {"preparing", "pasted", "submitting", "submitted", "cleared"}:
        state = "unknown"
    auto_enter = record.get("auto_enter")
    if not isinstance(auto_enter, bool):
        auto_enter = None
    context = record.get("context")
    if not isinstance(context, str) or not re.fullmatch(r"[0-9a-f]{64}", context):
        context = None
    return {"found": True, "matched": True, "request_id": rid, "fingerprint": fingerprint,
            "state": state, "auto_enter": auto_enter, "context": context,
            "completed": state == "submitted" or state == "pasted" and auto_enter is False}


def cleanup_attachment_draft(request):
    """Clear only a recorded, unsubmitted draft whose text and files still match."""
    import remote
    rid = request.get("request_id", "")
    if not re.fullmatch(r"[0-9a-f]{32}", str(rid)):
        raise DesktopError("附件草稿 ID 无效。")
    ledger = ROOT / "desktop-deliveries" / (rid + ".json")
    with (ROOT / "desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DesktopError("客户端正在处理其他操作，请稍后再清理草稿。") from None
        if not ledger.is_file():
            raise DesktopError("未找到本次附件草稿记录。")
        record = json.loads(ledger.read_text(encoding="utf-8"))
        if record.get("state") != "pasted" or record.get("auto_enter") is not False or not record.get("attachment_paths"):
            raise DesktopError("该记录不是可核对的未提交附件草稿。")
        context = record["context"]
        if request.get("context") != context:
            raise DesktopError("附件草稿的目标会话已变化，已停止清理。")
        paths = list(record["attachment_paths"])
        current = status()
        draft = current["draft"]
        if hashlib.sha256(draft.encode()).hexdigest() != record.get("draft_sha256"):
            raise DesktopError("草稿内容已修改，已停止清理。")
        require_attachment_draft(current, context, paths, draft)
        for path in list(paths):
            current = status()
            matched = require_attachment_draft(current, context, paths, draft)
            page, attrs = document()
            is_code = code_page(attrs)
            entry = composer(page, attrs)
            controls = code_dock(entry) if is_code else page
            target = new_code_target(attrs, controls) if is_code and current.get("new_session") else None
            actual_context = conversation_context(current["process_id"], attrs["URI"], target)
            if actual_context != context or text(entry).strip() != draft:
                raise DesktopError("移除附件前客户端会话或草稿已经变化，已停止清理。")
            removers = native_attachment_controls(controls, is_code)
            label = matched[path]
            control = removers.get(label)
            if control is None or not control.queryAction().doAction(0):
                raise DesktopError("客户端未接受本次附件的移除操作，已停止清理。")
            paths.remove(path)
            for _ in range(30):
                current = status()
                remaining = require_attachment_draft(current, context, paths + [path], draft, partial=True)
                if path not in remaining:
                    require_attachment_draft(current, context, paths, draft)
                    break
                time.sleep(.1)
            else:
                raise DesktopError("本次附件的移除结果未确认，已停止清理。")
        focus_composer(context, expected_draft=draft)
        require_attachment_draft(status(), context, [], draft)
        xcommand("key", "--clearmodifiers", "ctrl+a", "BackSpace")
        for _ in range(30):
            current = status()
            if (current["context"] != context or current["attachments"] or current["busy"] or current["dialogs"]
                    or current["approval"] or current.get("awaiting_input", False) or current.get("attachment_pending")):
                raise DesktopError("清理时客户端状态已经变化，请查看 Ubuntu。")
            if not current["draft"]:
                record.update(state="cleared", cleared_at=time.time())
                remote.atomic(ledger, record)
                return {"state": "cleared", "request_id": rid, "context": context, "attachments": []}
            if current["draft"] != draft:
                raise DesktopError("清理时草稿发生了其他变化，请查看 Ubuntu。")
            time.sleep(.1)
        raise DesktopError("本次文字草稿的清理结果未确认，请查看 Ubuntu。")


def prepare_message_submission(initial, current, record, value, paths):
    """Read-only pre-submit checks; no Send/Return has been attempted here."""
    allow_running = initial.get("allow_running") is True
    running_delivery = bool(initial.get("busy") or current.get("busy"))
    if running_delivery and (not allow_running or paths):
        error = DesktopError("当前运行时仅支持文字追加，已保留客户端草稿；原任务会继续运行。")
        error.not_submitted = True
        raise error
    atspi = initialize()
    page, attrs = document()
    is_code = initial.get("mode") == "Code"
    root = code_dock(composer(page, attrs)) if is_code else page
    labels = ({"Send", "Send message", "Send prompt", "Queue", "Queue message", "Queue prompt", "Add to queue"}
              if is_code else {"Send message"})
    for _ in range(20 if running_delivery else 200):
        pump_events()
        candidates = [n for n in buttons(root) if n.name in labels and showing(n)]
        if len(candidates) > 1:
            raise DesktopError("检测到多个发送按钮，已保留客户端草稿。")
        send_button = candidates[0] if candidates else None
        state = send_button.getState() if send_button is not None else None
        if state is not None and state.contains(atspi.STATE_ENABLED) and state.contains(atspi.STATE_SENSITIVE):
            break
        time.sleep(.15)
    else:
        if not running_delivery:
            raise DesktopError("附件尚未就绪或发送按钮不可用，已保留客户端草稿。")
        send_button = None
    current = status()
    require_attachment_draft(current, initial["context"], paths, value.strip(), allow_running=allow_running)
    running_delivery = running_delivery or bool(current.get("busy"))
    if (current["context"] != initial["context"] or current["draft"] != value.strip()
            or current["dialogs"] or current["approval"] or current.get("awaiting_input", False)
            or current["busy"] and not allow_running
            or send_button is not None and not showing(send_button)):
        raise DesktopError("提交前客户端内容已改变，已停止发送。")
    baseline = None
    if running_delivery:
        if not allow_running or paths:
            raise DesktopError("当前运行时仅支持文字追加，已保留客户端草稿。")
        application = app()
        current_page, current_attrs = document(application)
        if page_context(application, current_page, current_attrs) != initial["context"]:
            raise DesktopError("追加消息目标已经变化，已保留草稿。")
        baseline = running_user_snapshot(code_panel(current_page))
        record["running_submission"] = {"schema": 2 if baseline.get("anchor_kind") == "tail_row" else 1, "process_id": initial["process_id"],
            **baseline, "expected_text_sha256": normalized_message_hash(value)}
        record["delivery_mode"] = "running_insert"
        if send_button is None:
            running_enter_ready(initial, value)
    return send_button, running_delivery, baseline


def send(request):
    import remote
    value = request.get("text", "")
    rid = request.get("request_id", "")
    if not re.fullmatch(r"[0-9a-f]{32}", rid) or not isinstance(value, str) or not value.strip() or len(value.encode()) > 120000 or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", value):
        raise DesktopError("发送 ID 或文本无效。")
    fingerprint = hashlib.sha256(json.dumps([value, request.get("attachment_batch"), request.get("context"), bool(request.get("enter", True))]).encode()).hexdigest()
    ledger = ROOT / "desktop-deliveries" / (rid + ".json")
    with (ROOT / "desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            error = DesktopError("客户端正在接收另一条指令。")
            # This fresh request did not acquire the input lock and has not
            # loaded/submitted anything. A pre-existing ID remains uncertain.
            error.not_submitted = not ledger.exists()
            raise error from None
        if ledger.exists():
            saved = json.loads(ledger.read_text())
            if saved["fingerprint"] == fingerprint and saved["state"] in {"submitted", "pasted"}:
                return {**saved, "repeated": True}
            raise DesktopError("上次操作结果未确认或发送 ID 已使用，请先查看 Ubuntu，不会自动重发。")
        try:
            initial = check_ready({**request, "for_send": True})
        except DesktopError as exc:
            exc.not_submitted = True
            raise
        if initial.get("busy") and request.get("attachment_batch"):
            error = DesktopError("Claude 正在运行时请先追加文字；附件请等当前任务完成后再载入。")
            error.not_submitted = True
            raise error
        if (initial.get("mode") == "Code" and request.get("attachment_batch")
                and not initial.get("capabilities", {}).get("native_attachments", False)):
            error = DesktopError(initial.get("capabilities", {}).get("attachment_reason")
                                 or "当前 Code 附件传输不可用，请到 Ubuntu 查看；尚未载入或提交。")
            error.not_submitted = True
            raise error
        try:
            paths = attachment_paths(request)
        except DesktopError as exc:
            exc.not_submitted = True
            raise
        record = {"request_id": rid, "fingerprint": fingerprint, "state": "preparing", "time": time.time(),
                  "context": initial["context"], "auto_enter": bool(request.get("enter", True)),
                  "attachment_paths": paths}
        remote.atomic(ledger, record)
        try:
            current = load_native_attachment_draft(value, paths, initial)
        except DesktopError as exc:
            # This code path has not invoked the client's Send action.
            exc.not_submitted = True
            raise
        record.update(state="pasted", attachments=[Path(p).name for p in paths],
                      native_labels=current["attachments"], draft_sha256=hashlib.sha256(value.strip().encode()).hexdigest())
        remote.atomic(ledger, record)
        if request.get("enter", True):
            is_code = initial.get("mode") == "Code"
            try:
                send_button, running_delivery, baseline = prepare_message_submission(initial, current, record, value, paths)
            except DesktopError as exc:
                exc.not_submitted = True
                raise
            record["state"] = "submitting"
            remote.atomic(ledger, record)
            if send_button is None:
                # One ordinary Return follows the native running-Prompt contract.
                # Never send Stop, Ctrl+Enter, or another Return after an unknown.
                if initial.get("input_transport") == "wayland":
                    keyboard = NativeKeyboard()
                    try:
                        running_enter_ready(initial, value)
                        keyboard.key("Return")
                    finally:
                        keyboard.close()
                else:
                    xcommand("key", "Return")
            elif not send_button.queryAction().doAction(0):
                raise DesktopError("客户端未接受发送操作，请查看 Ubuntu。")
            deadline = time.monotonic() + (45 if initial.get("new_session") else 20)
            created_context = None
            while time.monotonic() < deadline:
                time.sleep(.2)
                try:
                    current = status()
                except DesktopError:
                    if initial.get("new_session"):
                        continue
                    raise
                if initial.get("new_session") and current.get("session_id"):
                    if created_context is not None and current["context"] != created_context:
                        raise DesktopError("首条消息提交后又切换了会话，结果未确认，请查看 Ubuntu。")
                    created_context = current["context"]
                confirmed = (running_submission_confirmed(initial, current, record["running_submission"]["expected_text_sha256"], baseline)
                             if running_delivery else code_submission_confirmed(initial, current, value) if is_code else
                             not current["draft"] and not current["attachments"])
                if confirmed:
                    record["state"] = "submitted"
                    if running_delivery:
                        record["submission_evidence"] = confirmed
                    if initial.get("new_session"):
                        record.update(initial_context=initial["context"], context=current["context"],
                                      session_id=current["session_id"], workspace=initial["workspace"])
                    remote.atomic(ledger, record)
                    break
            else:
                raise DesktopError("发送结果未确认，请查看客户端；不会自动重发。")
        return record


def desktop_executable(request):
    configured = request.get("desktop_bin") or "claude-desktop"
    resolved = shutil.which(configured) if "/" not in configured else None
    executable = Path(resolved or configured).expanduser()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DesktopError("Claude 桌面客户端启动路径不存在，请在 Ubuntu / SSH 设置中填写实际可执行文件路径。")
    return executable.resolve()



def launch_client(request):
    executable = desktop_executable(request)
    env = environment()
    env.update(GDK_BACKEND="x11", ELECTRON_OZONE_PLATFORM_HINT="x11")
    # A second normal launch restores Electron's existing window through its single-instance handler.
    subprocess.Popen([str(executable), "--force-renderer-accessibility", "--ozone-platform=x11"],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def start(request):
    initialize()
    if desktop_locked():
        raise DesktopError("Ubuntu 桌面已锁定，请先在 Ubuntu 解锁。")
    try:
        activate()
        return {"opened": True, "restored": False}
    except (ClientUnavailable, WindowUnavailable):
        launch_client(request)
    for _ in range(100):
        time.sleep(.2)
        try:
            activate()
            return {"opened": True, "restored": True}
        except (ClientUnavailable, WindowUnavailable):
            pass
    raise DesktopError("未找到 Bridge 可输入的窗口。请在 Ubuntu 从 Claude 菜单完全退出应用，再点击 Bridge 的“打开 / 显示客户端”，然后打开原来的会话。")


def setup(request):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not all((TOOLS / p).exists() for p in ["usr/bin/xdotool", "usr/bin/xclip", "usr/lib/python3/dist-packages/Xlib"]):
        TOOLS.mkdir(exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            p = subprocess.run(["apt-get", "download", "xdotool", "libxdo3", "xclip", "python3-xlib"], cwd=directory,
                               capture_output=True, timeout=110)
            if p.returncode:
                raise DesktopError("Ubuntu 桌面依赖下载失败，请检查 Ubuntu 仓库网络。")
            for deb in Path(directory).glob("*.deb"):
                subprocess.run(["dpkg-deb", "-x", str(deb), str(TOOLS)], check=True, capture_output=True, timeout=15)
    try:
        initialize()
        from PIL import Image
    except ImportError:
        raise DesktopError("Ubuntu 需要系统包 python3-pyatspi 和 python3-pil。")
    subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"], env=environment(), check=True, capture_output=True, timeout=8)
    ensure_workspace(request)
    launcher = ROOT / "start-desktop.sh"
    import shlex
    executable = str(desktop_executable(request))
    launcher.write_text('#!/bin/sh\nexport GDK_BACKEND=x11 ELECTRON_OZONE_PLATFORM_HINT=x11\nexec ' + shlex.quote(executable) + ' --force-renderer-accessibility --ozone-platform=x11 "$@"\n')
    launcher.chmod(0o700)
    shortcut = Path.home() / ".local/share/applications/claude-bridge-desktop.desktop"
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    shortcut.write_text('[Desktop Entry]\nType=Application\nName=Claude (Bridge)\nExec="' + str(launcher) + '"\nIcon=claude-desktop\nTerminal=false\n')
    return {"version": VERSION, "transport": "desktop", "configured": True}


def dispatch(request):
    action = request.get("action")
    if action in {"desktop_frame", "desktop_input", "desktop_input_status", "desktop_close"}:
        import desktop_stream
        try:
            return desktop_stream.dispatch(request, root=ROOT, environment=environment())
        except desktop_stream.StreamError as exc:
            raise DesktopError(str(exc)) from None
    if action == "setup":
        return setup(request)
    if action == "start":
        with (ROOT / "desktop.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                error = DesktopError("客户端正在处理其他桌面操作，请稍后再显示客户端。")
                error.not_submitted = True
                raise error from None
            return start(request)
    if action == "probe":
        state = status()
        return {"version": VERSION, "transport": "desktop", **state}
    if action == "list_projects":
        return list_projects()
    if action == "match_current":
        return match_current(request)
    if action == "project_catalog":
        return read_project_catalog(request)
    if action == "open_project":
        return open_project(request)
    if action == "switch_project":
        return switch_project(request)
    if action == "check_ready":
        ensure_workspace(request)
        return check_ready(request)
    if action == "send":
        return send(request)
    if action == "cleanup_attachment_draft":
        return cleanup_attachment_draft(request)
    if action == "delivery_status":
        return delivery_status(request)
    if action == "new":
        with (ROOT / "desktop.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                error = DesktopError("客户端正在处理其他桌面操作，请稍后再新建对话。")
                error.not_submitted = True
                raise error from None
            current = check_ready({**request, "for_send": False})
            page, _ = document()
            button = find("push button", "New", page)
            if not showing(button) or not button.queryAction().doAction(0):
                raise DesktopError("未能打开新会话。请在 Ubuntu 的 Claude 客户端点击 New。")
            return {"opened": True, "mode": current["mode"]}
    if action == "capture":
        application = app()
        page, attrs = document(application)
        context = page_context(application, page, attrs)
        if request.get("context") and request["context"] != context:
            raise DesktopError("Ubuntu 当前会话已经变化，已停止读取旧会话内容。")
        is_code = code_page(attrs)
        if is_code:
            page = code_panel(page)
        route = urlsplit(attrs["URI"]).path
        question = pending_question(page) if is_code else None
        # The waiting card is the newest actionable content. Return it directly
        # rather than reading an entire turn or waiting for its user anchor.
        reply = ({"text": question_reply_text(question), "reply_count": 1, "message_count": 1,
                  "code_blocks": [], "scope": "latest", "read_status": "ready", "partial": False,
                  "read_reason": QUESTION_WAIT_REASON}
                 if question is not None else current_turn_reply(page, context))
        if reply.get("read_status") == "unavailable" and question is None and (CODE_LOCAL_SESSION.fullmatch(route)
                or re.fullmatch(r"/chat/[0-9a-f-]{36}/?", route, re.IGNORECASE)):
            raise DesktopError(reply.get("read_reason") or "当前会话的最后一条回复尚未载入，请稍后刷新。")
        result = {"context": context, "route": route,
                  "title": attrs.get("Title", "Claude"), "mode": "Code" if is_code else "Chat",
                  "generating": False if question is not None else reply_in_progress(page),
                  "awaiting_input": question is not None, "question": question, **reply}
        confirm_page_context(context)
        return result
    raise DesktopError("不支持的客户端操作。")
