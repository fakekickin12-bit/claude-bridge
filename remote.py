#!/usr/bin/env python3
"""User-owned Ubuntu tmux bridge; JSON stdin/stdout, no listening TCP ports."""
from __future__ import annotations

import hashlib
import base64
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

if __name__ == "__main__":
    sys.modules["remote"] = sys.modules[__name__]

ROOT = Path.home() / ".local/share/claude-bridge"
SOCKET = ROOT / "tmux.sock"
THIRD = ROOT / "third_party"
VERSION = "1.6.0"


class RemoteError(Exception):
    pass


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        tmp = f.name
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def tmux_path():
    local = THIRD / "usr/bin/tmux"
    return shutil.which("tmux") or (str(local) if local.exists() else None)


def tmux(args, data=None, check=True):
    binary = tmux_path()
    if not binary:
        raise RemoteError("缺少 tmux。请先在 Windows 工具中点击“配置 Ubuntu”。")
    env = os.environ.copy()
    env.pop("TMUX", None)
    env["LD_LIBRARY_PATH"] = str(THIRD / "usr/lib/x86_64-linux-gnu") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    p = subprocess.run([binary, "-S", str(SOCKET), "-f", "/dev/null", *args], input=data,
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=12)
    if p.returncode and check:
        raise RemoteError("tmux：" + p.stderr.strip()[:1200])
    return p


def setup():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ROOT, 0o700)
    if not tmux_path():
        if not shutil.which("apt-get") or not shutil.which("dpkg-deb"):
            raise RemoteError("请先在 Ubuntu 安装 tmux；自动配置需要 apt-get 和 dpkg-deb。")
        THIRD.mkdir(exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="packages-", dir=ROOT) as td:
            packages = ["tmux", "libutempter0", "libevent-core-2.1-7"]
            p = subprocess.run(["apt-get", "download", *packages], cwd=td, capture_output=True, text=True, timeout=110)
            if p.returncode:
                raise RemoteError("下载 Ubuntu 仓库依赖失败，请检查 Ubuntu 网络或手动安装 tmux：" + p.stderr[-600:])
            for deb in Path(td).glob("*.deb"):
                subprocess.run(["dpkg-deb", "-x", str(deb), str(THIRD)], check=True, capture_output=True, timeout=15)
    result = tmux(["-V"])
    return {"version": VERSION, "tmux": result.stdout.strip(), "path": str(ROOT)}


def session_name(request):
    name = request.get("session", "")
    if not re.fullmatch(r"bridge-[A-Za-z0-9_-]{1,60}", name):
        raise RemoteError("会话名必须以 bridge- 开头，只能包含字母、数字、下划线和短横线。")
    return name


def exists(name):
    return tmux(["has-session", "-t", "=" + name], check=False).returncode == 0


def pane_info(name):
    if not exists(name):
        raise RemoteError("会话尚未启动，请先点击“启动 / 显示终端”。")
    info = tmux(["list-panes", "-t", "=" + name, "-F", "#{pane_id}|#{pane_dead}|#{pane_current_command}"]).stdout.strip().splitlines()
    if len(info) != 1:
        raise RemoteError("目标会话包含多个窗格，已停止发送。请新建独立会话。")
    pane, dead, command = info[0].split("|", 2)
    if dead == "1":
        raise RemoteError("Claude Code 已退出。请新建会话，或在 Ubuntu 中使用 Claude 的 /resume 恢复。")
    return pane, command


def desktop_env():
    env = os.environ.copy()
    runtime = Path("/run/user") / str(os.getuid())
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(runtime / "bus")
    ways = [p for p in runtime.glob("wayland-*") if not p.name.endswith(".lock")]
    if ways:
        env["WAYLAND_DISPLAY"] = ways[0].name
        env["XDG_SESSION_TYPE"] = "wayland"
    env.setdefault("DISPLAY", ":0")
    return env


def show(name):
    pane_info(name)
    terminal = shutil.which("gnome-terminal")
    if not terminal:
        raise RemoteError("未找到 gnome-terminal。会话仍在后台；可用 SSH 附加查看。")
    env = desktop_env()
    env["LD_LIBRARY_PATH"] = str(THIRD / "usr/lib/x86_64-linux-gnu") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    p = subprocess.run([terminal, "--title=Claude Bridge | " + name, "--", tmux_path(), "-S", str(SOCKET),
                        "attach-session", "-t", "=" + name], capture_output=True, text=True, env=env, timeout=15)
    if p.returncode:
        raise RemoteError("桌面终端未能打开；tmux 会话仍保留。" + p.stderr[:400])
    return {"session": name, "desktop": True}


def start(request):
    name = session_name(request)
    meta_file = ROOT / "sessions" / (name + ".json")
    cwd = Path(request.get("cwd", "")).expanduser()
    if not cwd.is_absolute():
        raise RemoteError("Ubuntu 工作目录必须是绝对路径或 ~/ 开头。")
    if exists(name):
        pane_info(name)
        if meta_file.exists() and json.loads(meta_file.read_text()).get("cwd") != str(cwd):
            raise RemoteError("该会话使用另一个工作目录。请切换回原目录或新建会话。")
        return show(name)
    binary = request.get("claude_bin", "").strip()
    resolved = str(Path(binary).expanduser()) if "/" in binary else shutil.which(binary)
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        raise RemoteError("找不到 Claude Code 可执行文件，请检查 SSH 设置中的路径。")
    cwd.mkdir(parents=True, exist_ok=True)
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {"session_id": str(uuid.uuid4()), "cwd": str(cwd)}
    args = [resolved, "--resume" if meta_file.exists() else "--session-id", meta["session_id"]]
    if request.get("claude_model", "").strip():
        args += ["--model", request["claude_model"].strip()]
    # exec means exiting Claude never leaves a shell capable of interpreting the next prompt.
    atomic(meta_file, meta)
    tmux(["new-session", "-d", "-s", name, "-c", str(cwd), "-x", "120", "-y", "38", "exec " + shlex.join(args),
          ";", "set-window-option", "-t", name + ":0", "remain-on-exit", "on"])
    time.sleep(0.5)
    return {**show(name), "session_id": meta["session_id"], "created": True}


def capture(name):
    pane, _ = pane_info(name)
    return tmux(["capture-pane", "-p", "-t", pane, "-S", "-160"]).stdout


def ready(screen):
    tail = "\n".join(screen.rstrip().splitlines()[-18:])
    # Never send Return to onboarding, trust, login, or permission dialogs.
    blocked = ["Do you trust", "trust this folder", "Yes, I trust", "Do you want to proceed", "Allow this",
               "Enter to confirm", "Choose the text style", "Select login method", "Sign in", "Press Enter to continue"]
    if any(x.casefold() in tail.casefold() for x in blocked):
        return False
    prompts = [line for line in tail.splitlines() if re.match(r"^\s*❯", line)]
    return bool(prompts and re.match(r"^\s*❯\s*(?:$|Try\s|Type\s|Ask\s)", prompts[-1]))


def send(request):
    name = session_name(request)
    with (ROOT / (name + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RemoteError("该会话正在接收另一条指令，请稍后查看终端。")
        return send_locked(request, name)


def send_locked(request, name):
    pane, command = pane_info(name)
    if "claude" not in command.casefold() and command != "node":
        raise RemoteError("目标窗格当前不是 Claude Code，已停止发送。")
    meta = ROOT / "sessions" / (name + ".json")
    if not meta.exists():
        raise RemoteError("目标不是由 Claude Bridge 创建的会话，已停止发送。")
    if request.get("attachment_batch"):
        verify_attachments(name, request["attachment_batch"])
    text = request.get("text", "")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 120000:
        raise RemoteError("发送文本为空或超过 120 KB。")
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
        raise RemoteError("文本包含终端控制字符，已停止发送。")
    rid = request.get("request_id", "")
    if not re.fullmatch(r"[0-9a-f]{32}", rid):
        raise RemoteError("无效的发送 ID。")
    fingerprint = hashlib.sha256(json.dumps([name, text, bool(request.get("enter", True))], ensure_ascii=False).encode()).hexdigest()
    ledger = ROOT / "deliveries" / (rid + ".json")
    if ledger.exists():
        saved = json.loads(ledger.read_text())
        if saved["fingerprint"] != fingerprint:
            raise RemoteError("发送 ID 已对应另一份内容。")
        if saved["state"] in ("submitted", "pasted"):
            return {**saved, "repeated": True}
        raise RemoteError("上次发送停在未确认状态，请先查看终端，不会自动重发。")
    if not ready(capture(name)):
        raise RemoteError("Claude 尚未处于空白输入状态。请在 Ubuntu 终端完成登录/目录信任/权限选择，或等待当前任务结束后再发送。")
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {"fingerprint": fingerprint, "request_id": rid, "session": name, "state": "preparing", "time": time.time()}
    # Exclusive create protects against two senders racing with the same delivery ID.
    try:
        with ledger.open("x", encoding="utf-8") as f:
            json.dump(record, f)
        os.chmod(ledger, 0o600)
    except FileExistsError:
        raise RemoteError("同一发送正在处理中，请查看终端。")
    buffer = "cb-" + rid
    try:
        tmux(["load-buffer", "-b", buffer, "-"], data=text)
        tmux(["paste-buffer", "-d", "-p", "-b", buffer, "-t", pane])
        record["state"] = "pasted"
        atomic(ledger, record)
        if request.get("enter", True):
            time.sleep(0.25)
            tmux(["send-keys", "-t", pane, "Enter"])
            record["state"] = "submitted"
            atomic(ledger, record)
    except Exception:
        raise RemoteError("发送过程未完整确认。请查看 Ubuntu 终端；不会自动重发。") from None
    return record


def attachment_manifest(name, batch_id):
    if not re.fullmatch(r"[0-9a-f]{32}", str(batch_id)):
        raise RemoteError("无效的附件批次。")
    return ROOT / "uploads" / (name + "-" + batch_id + ".json")


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def attachment_prepare(request):
    if request.get("transport") == "desktop":
        from desktop_remote import ensure_workspace
        ensure_workspace(request)
    name = session_name(request)
    batch = request.get("batch_id", "")
    record_path = attachment_manifest(name, batch)
    meta_file = ROOT / "sessions" / (name + ".json")
    if not meta_file.is_file():
        raise RemoteError("请先启动 Claude Bridge 会话，再发送附件。")
    cwd = Path(json.loads(meta_file.read_text())["cwd"]).resolve()
    if not cwd.is_dir():
        raise RemoteError("Claude 会话工作目录不存在。")
    directory = (cwd / "ClaudeBridgeAttachments" / batch).resolve()
    if not directory.is_relative_to(cwd):
        raise RemoteError("附件目录不能通过符号链接指向工作区之外。")
    files = request.get("files", [])
    if not isinstance(files, list) or not 1 <= len(files) <= 256:
        raise RemoteError("附件清单为空或过长。")
    ids, total = set(), 0
    normalized = []
    for item in files:
        fid, digest, size = item.get("id", ""), item.get("sha256", ""), item.get("size")
        title = item.get("name", "")
        if not re.fullmatch(r"[0-9a-f]{32}", fid) or fid in ids or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RemoteError("附件 ID 或校验值无效。")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not isinstance(title, str) or len(title) > 1024 or re.search(r"[\x00-\x1f\x7f]", title):
            raise RemoteError("附件名称或大小无效。")
        suffix = Path(title).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
            suffix = ".bin"
        ids.add(fid)
        total += size
        normalized.append({**item, "path": str(directory / (fid + suffix))})
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if record_path.exists():
        saved = json.loads(record_path.read_text())
        if saved["files"] != normalized:
            raise RemoteError("该附件批次已对应另一份清单，不会覆盖。")
    else:
        if shutil.disk_usage(directory).free < total + 32 * 1024 * 1024:
            raise RemoteError("Ubuntu 工作区可用空间不足，附件尚未上传。")
        atomic(record_path, {"session": name, "batch_id": batch, "files": normalized, "complete": False})
    completed = [f["id"] for f in normalized if Path(f["path"]).is_file() and Path(f["path"]).stat().st_size == f["size"] and file_hash(Path(f["path"])) == f["sha256"]]
    return {"batch_id": batch, "completed_ids": completed}


def load_attachment_batch(name, batch):
    record = attachment_manifest(name, batch)
    if not record.is_file():
        raise RemoteError("附件批次尚未准备。")
    data = json.loads(record.read_text())
    if data.get("session") != name:
        raise RemoteError("附件不属于当前会话。")
    return record, data


def guarded_attachment_path(name, value):
    meta = ROOT / "sessions" / (name + ".json")
    cwd = Path(json.loads(meta.read_text())["cwd"]).resolve()
    path = Path(value)
    if path.is_symlink() or not path.resolve().is_relative_to(cwd):
        raise RemoteError("附件路径已改变或指向工作区之外。")
    return path


def receive(request, stream):
    name = session_name(request)
    record, data = load_attachment_batch(name, request.get("batch_id", ""))
    entry = next((f for f in data["files"] if f["id"] == request.get("file_id")), None)
    if not entry:
        raise RemoteError("附件不在已确认的上传清单中。")
    destination = guarded_attachment_path(name, entry["path"])
    # All published paths come from a guarded manifest, never from receiver arguments.
    if destination.is_symlink() or destination.parent.is_symlink():
        raise RemoteError("附件目标不能是符号链接。")
    fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=destination.parent)
    temporary = Path(temporary)
    digest = hashlib.sha256()
    remaining = entry["size"]
    try:
        with os.fdopen(fd, "wb") as f:
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise RemoteError("附件上传中断，原件没有发布。")
                digest.update(block)
                f.write(block)
                remaining -= len(block)
            if stream.read(1):
                raise RemoteError("附件大小与清单不符，原件没有发布。")
            f.flush()
            os.fsync(f.fileno())
        if digest.hexdigest() != entry["sha256"]:
            raise RemoteError("附件 SHA-256 不匹配，原件没有发布。")
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, destination)  # Exclusive publish: never overwrite an existing attachment.
        except FileExistsError:
            if destination.stat().st_size != entry["size"] or file_hash(destination) != entry["sha256"]:
                raise RemoteError("目标已有不同内容，不会覆盖。")
        return {"id": entry["id"], "path": str(destination), "size": entry["size"], "sha256": entry["sha256"]}
    finally:
        temporary.unlink(missing_ok=True)


def attachment_finish(request):
    name = session_name(request)
    record, data = load_attachment_batch(name, request.get("batch_id", ""))
    stats = {}
    for entry in data["files"]:
        path = guarded_attachment_path(name, entry["path"])
        if not path.is_file() or path.is_symlink() or path.stat().st_size != entry["size"] or file_hash(path) != entry["sha256"]:
            raise RemoteError("附件尚未完整上传或校验失败，指令不会发送。")
        stats[entry["id"]] = path.stat().st_mtime_ns
    data.update(complete=True, verified_mtimes=stats)
    atomic(record, data)
    return {"batch_id": data["batch_id"], "files": data["files"]}


def verify_attachments(name, batch):
    _, data = load_attachment_batch(name, batch)
    if not data.get("complete"):
        raise RemoteError("附件尚未校验完成，指令不会发送。")
    for entry in data["files"]:
        path = guarded_attachment_path(name, entry["path"])
        if not path.is_file() or path.is_symlink() or path.stat().st_size != entry["size"] or path.stat().st_mtime_ns != data["verified_mtimes"].get(entry["id"]):
            raise RemoteError("附件在上传后发生变化，指令不会发送。")


def check_ready(request):
    name = session_name(request)
    _, command = pane_info(name)
    if "claude" not in command.casefold() and command != "node":
        raise RemoteError("目标窗格当前不是 Claude Code，已停止发送。")
    if not ready(capture(name)):
        raise RemoteError("Claude 尚未处于空白输入状态。请先在 Ubuntu 处理当前任务、目录信任或权限选择。")
    return {"ready": True}


def dispatch(request):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    action = request.get("action")
    if request.get("transport") == "desktop" and action in {"setup", "probe", "start", "new", "capture", "check_ready", "send", "list_projects", "project_catalog", "match_current", "open_project", "switch_project", "cleanup_attachment_draft", "delivery_status", "desktop_frame", "desktop_input", "desktop_input_status", "desktop_close"}:
        import desktop_remote
        try:
            return desktop_remote.dispatch(request)
        except desktop_remote.DesktopError as exc:
            error = RemoteError(str(exc))
            error.not_submitted = bool(getattr(exc, "not_submitted", False))
            raise error from None
    if action == "setup":
        return setup()
    if action == "probe":
        return {"version": VERSION, "tmux": bool(tmux_path()), "terminal": bool(shutil.which("gnome-terminal")),
                "sessions": tmux(["list-sessions", "-F", "#{session_name}"], check=False).stdout.splitlines() if tmux_path() else []}
    if action == "start":
        return start(request)
    if action == "attachment_prepare":
        return attachment_prepare(request)
    if action == "attachment_finish":
        return attachment_finish(request)
    if action == "check_ready":
        return check_ready(request)
    name = session_name(request)
    if action == "show":
        return show(name)
    if action == "capture":
        return {"session": name, "text": capture(name)}
    if action == "send":
        return send(request)
    raise RemoteError("不支持的操作。")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--receive":
            request = json.loads(base64.urlsafe_b64decode(sys.argv[2]))
            result = receive(request, sys.stdin.buffer)
        else:
            request = json.loads(sys.stdin.read(300000))
            result = dispatch(request)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    except Exception as exc:
        from desktop_remote import DesktopError
        message = str(exc) if isinstance(exc, (RemoteError, DesktopError)) else "远端配置或执行失败：" + type(exc).__name__
        print(json.dumps({"ok": False, "error": message, "not_submitted": bool(getattr(exc, "not_submitted", False))}, ensure_ascii=False))
