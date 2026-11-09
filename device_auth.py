"""Local device credentials shared by desktop settings and the mobile worker."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import core

PAIR_CODE_TTL = 24 * 3600
DEVICE_TTL = 180 * 86400
DEVICE_COOKIE = "__Host-bridge-device"
ID_PATTERN = re.compile(r"[0-9a-f]{32}")


def pairing_url(data_dir=None):
    """Only pair against the operator's explicitly configured HTTPS origin."""
    settings = core.mobile_settings(data_dir)
    value = os.environ.get("CLAUDE_BRIDGE_ORIGIN", settings.get("public_origin", ""))
    if not isinstance(value, str):
        raise core.BridgeError("请先在手机端 settings.json 配置自己的 public_origin HTTPS 地址。")
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        valid = (not any(character.isspace() or ord(character) < 32 for character in value)
                 and parsed.scheme == "https" and parsed.hostname and parsed.path == ""
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", parsed.hostname)
                 and parsed.hostname != "example.com" and not parsed.hostname.endswith(".example.com"))
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise core.BridgeError("请先配置自己的 HTTPS 手机入口：设置 CLAUDE_BRIDGE_ORIGIN，或在 data/mobile/settings.json 填写 public_origin。")
    return value


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def restrict_local_access(directory, files=()):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import subprocess
        account = subprocess.check_output(["whoami"], text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip()
        result = subprocess.run(["icacls", str(directory), "/inheritance:r", "/grant:r", account + ":(OI)(CI)F",
                                 "/Q"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise core.BridgeError("网页登录文件已生成，但无法限定本机访问权限。请检查后再开放网页。")
        for file in files:
            result = subprocess.run(["icacls", str(file), "/inheritance:r", "/grant:r", account + ":F", "/Q"],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise core.BridgeError("无法限定网页登录文件的本机访问权限。请检查后再开放网页。")
    else:
        directory.chmod(0o700)
        for file in files:
            Path(file).chmod(0o600)


def initialize_access(data_dir):
    """Retain the legacy local file helper; these passwords no longer grant web access."""
    directory = Path(data_dir) / "mobile"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "access.json"
    if target.exists():
        raise core.BridgeError("网页登录已配置。如需更改，请明确执行密码重置。")
    restrict_local_access(directory)
    password = "cb-" + secrets.token_urlsafe(18)
    salt = secrets.token_hex(16)
    core.atomic_json(target, {"salt": salt, "hash": password_hash(password, salt)})
    note = directory / "手机网页登录信息.txt"
    note.write_text("旧版登录信息（当前网页仅允许登记设备）\n\n旧版密码：" + password + "\n", encoding="utf-8")
    restrict_local_access(directory, (target, note))
    return note


def create_device_tables(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS device_pairings (
            code_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL,
            consumed_at REAL, device_id TEXT);
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, csrf TEXT NOT NULL, label TEXT NOT NULL,
            created_at REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0);
    """)


@contextmanager
def device_database(data_dir):
    """Local device management must not initialize or change the worker job queue."""
    directory = Path(data_dir) / "mobile"
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "bridge.sqlite3", timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        create_device_tables(connection)
        with connection:
            yield connection
    finally:
        connection.close()


def issue_pairing_code(data_dir):
    origin = pairing_url(data_dir)
    directory = Path(data_dir) / "mobile"
    restrict_local_access(directory)
    note = directory / "设备配对信息.txt"
    code = "cb-pair-" + secrets.token_urlsafe(18)
    now = time.time()
    expires = now + PAIR_CODE_TTL
    body = ("Claude Bridge 设备白名单\n\n" + origin + "\n\n配对码：" + code +
            "\n\n有效期至：" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires)) +
            "\n有效期 24 小时，仅能登记一个浏览器设备。\n设备凭证有效期 180 天，可在 Windows 设备管理中撤销。\n")
    with device_database(data_dir) as db:
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".pairing-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(temporary, note)
        finally:
            Path(temporary).unlink(missing_ok=True)
        restrict_local_access(directory, (directory / "bridge.sqlite3", note))
        db.execute("INSERT INTO device_pairings(code_hash,created_at,expires_at) VALUES (?,?,?)",
                   (hashlib.sha256(code.encode()).hexdigest(), now, expires))
    return note


def list_devices(data_dir):
    with device_database(data_dir) as db:
        rows = db.execute("SELECT id,label,created_at,last_seen,expires_at,revoked FROM devices ORDER BY created_at DESC").fetchall()
    return [{**dict(row), "revoked": bool(row["revoked"])} for row in rows]


def revoke_device(data_dir, device_id):
    if not ID_PATTERN.fullmatch(device_id):
        raise core.BridgeError("设备标识无效，请在设备列表中选择。")
    with device_database(data_dir) as db:
        changed = db.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,)).rowcount
        if not changed:
            raise core.BridgeError("未找到已登记的设备。")
