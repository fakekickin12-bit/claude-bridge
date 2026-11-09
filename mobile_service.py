"""Authenticated mobile UI backed by the existing Windows Bridge configuration."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import core
from device_auth import (DEVICE_COOKIE, DEVICE_TTL, PAIR_CODE_TTL,
                         create_device_tables, device_database, initialize_access,
                         issue_pairing_code, list_devices, password_hash,
                         restrict_local_access, revoke_device)
from media import build_bundle, compose_prompt, kind, upload_bundle
from reply_translations import ReplyCache, translation_key, translation_revision

WEB = Path(__file__).parent / "mobile_web"
TERMINAL_STATES = {"translated", "pasted", "submitted", "failed", "unknown"}
ID_PATTERN = re.compile(r"[0-9a-f]{32}")
MAX_UPLOAD = 256 * 1024 * 1024


class RequestSizeLimit:
    """Bound streamed bodies before multipart parsing creates temporary files."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = MAX_UPLOAD + 65536 if scope["path"] == "/api/uploads" else 1024 * 1024
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = 0
        if length > limit:
            return await JSONResponse({"detail": "请求过大，单个附件请控制在 256 MB 内。"}, status_code=413)(scope, receive, send)
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, "请求过大，单个附件请控制在 256 MB 内。")
            return message

        await self.app(scope, bounded_receive, send)


class JobRequest(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    action: Literal["translate", "send", "translate_send", "new", "translate_reply", "switch_project", "open_project", "match_current"]
    profile: str = Field(default="", max_length=200)
    source: str = Field(default="", max_length=120000)
    english: str = Field(default="", max_length=120000)
    auto_enter: bool = True
    expected_context: str = Field(default="", max_length=64)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    source_hash: str = Field(default="", max_length=64)
    retry: bool = False
    catalog_id: str = Field(default="", max_length=64)
    project_id: str = Field(default="", max_length=64)
    session_id: str = Field(default="", max_length=64)
    session_catalog_id: str = Field(default="", max_length=64)


class LoginRequest(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    code: str = Field(min_length=1, max_length=200)
    label: str = Field(default="浏览器设备", min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")


@contextmanager
def service_instance(data_dir):
    directory = Path(data_dir) / "mobile"
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "worker.lock").open("a+b")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise core.BridgeError("手机网页后台已在运行。") from None
    try:
        yield
    finally:
        handle.close()


class MobileBridge:
    def __init__(self, data_dir, *, ssh_factory=core.SSH, translator_factory=None):
        self.data_dir = Path(data_dir)
        self.directory = self.data_dir / "mobile"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.directory / "bridge.sqlite3"
        self.ssh_factory = ssh_factory
        self.translator_factory = translator_factory
        self.lock = threading.RLock()
        self.remote_lock = threading.Lock()
        self.event = threading.Event()
        self.stop_event = threading.Event()
        self.threads = []
        self.worker_error = False
        self.last_seen = 0.0
        self.active_job = None
        self.target = {"ready": False, "reason": "正在连接 Ubuntu…"}
        self.reply = {"text": "", "source_hash": "", "mode": "", "context": "", "loading": False,
                      "generating": False, "updated_at": 0}
        self.code_blocks = []
        self.reply_cache = ReplyCache(self.data_dir / "reply-translations")
        self.login_failures = deque()
        self.project_catalog = {"projects": [], "loading": False, "reason": ""}
        self.projects_initialized = False
        self.projects_requested = threading.Event()
        self.capture_wake = threading.Event()
        self.last_target_probe = 0.0
        self.target_probe_context = ""
        self.target_probe_connection = ""
        self.receipt_due = {}
        self.receipt_lock = threading.Lock()
        self.receipt_event = threading.Event()
        with self.database() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, phase TEXT NOT NULL, result TEXT, error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS reply_attempts (key TEXT PRIMARY KEY, error TEXT NOT NULL);
            """)
            create_device_tables(db)
            db.execute("UPDATE jobs SET state='unknown', error=?, updated_at=? WHERE state='running'",
                       ("后台曾在操作中重启，请检查 Ubuntu 和发送记录后再决定下一步。", time.time()))

    @contextmanager
    def database(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def config(self):
        return core.load_config(self.data_dir / "config.json")

    def translator(self, profile_id, profile=None):
        if profile is None:
            config = self.config()
            if profile_id not in config["profiles"]:
                raise core.BridgeError("翻译连接已变化，请重新选择。")
            profile = config["profiles"][profile_id]
        profile = dict(profile)
        if self.translator_factory:
            return self.translator_factory(profile_id, profile)
        if profile.get("kind") == "codex":
            return core.CodexTranslator(profile)
        key = core.unprotect_key(profile.get("key_encrypted", "")) or os.environ.get(profile.get("key_env", ""), "")
        return core.Translator(profile, key)

    def job(self, job_id):
        with self.database() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "未找到这次操作。")
        payload = json.loads(row["payload"])
        result = json.loads(row["result"]) if row["result"] else None
        if row["state"] == "unknown" and payload["action"] in {"send", "translate_send"}:
            self.request_receipt(job_id)
        public_result = {k: v for k, v in result.items() if k != "delivery"} if result else result
        return {"id": row["id"], "action": payload["action"], "profile": payload.get("profile", ""), "source": payload.get("source", ""),
                "english": (result or {}).get("english", payload.get("english", "")),
                "attachment_ids": payload.get("attachment_ids", []),
                "state": row["state"], "phase": row["phase"], "result": public_result,
                "error": row["error"], "created_at": row["created_at"]}

    def enqueue(self, request):
        payload = request.model_dump()
        config = self.config()
        if payload["action"] not in {"new", "switch_project", "open_project", "match_current"} and payload["profile"] not in config["profiles"]:
            raise HTTPException(400, "请选择已有的翻译连接。")
        if payload["action"] == "match_current" and any(payload[key] for key in (
                "source", "english", "attachment_ids", "expected_context", "catalog_id", "project_id", "session_id", "session_catalog_id")):
            raise HTTPException(400, "匹配当前对话只读取 Ubuntu 当前会话，不携带草稿或附件。")
        if payload["action"] in {"send", "translate_send", "new", "switch_project", "open_project"}:
            if not re.fullmatch(r"[0-9a-f]{64}", payload["expected_context"]):
                raise HTTPException(409, "目标会话尚未确认，请等连接状态更新后再发送。")
        if payload["action"] in {"switch_project", "open_project"}:
            if (not re.fullmatch(r"[0-9a-f]{64}", payload["catalog_id"])
                    or not re.fullmatch(r"[0-9a-f]{64}", payload["project_id"])
                    or payload["session_id"] and not re.fullmatch(r"[0-9a-f]{64}", payload["session_id"])):
                raise HTTPException(400, "项目目录已变化，请重新读取后选择。")
            if payload["action"] == "switch_project" and payload["session_id"] and not re.fullmatch(r"[0-9a-f]{64}", payload["session_catalog_id"]):
                raise HTTPException(400, "请先选择项目，读取该项目的会话后再进入。")
            if payload["action"] == "open_project" and payload["session_id"]:
                raise HTTPException(400, "打开项目时不指定其他项目的会话。")
            if payload["attachment_ids"]:
                raise HTTPException(400, "切换项目不能携带附件。")
        if payload["action"] in {"translate", "translate_send"}:
            core.validate_text(payload["source"])
        if payload["action"] == "send":
            core.validate_text(payload["english"])
        for attachment_id in payload["attachment_ids"]:
            if not ID_PATTERN.fullmatch(attachment_id):
                raise HTTPException(400, "附件标识无效，请重新选择附件。")
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        canonical = dict(payload)
        if not canonical["session_catalog_id"]:
            canonical.pop("session_catalog_id")
        fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        now = time.time()
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT fingerprint FROM jobs WHERE id=?", (payload["id"],)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise HTTPException(409, "这次操作的内容已变化，请使用新的操作。")
            else:
                waiting = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
                if waiting >= 8:
                    raise HTTPException(429, "等待中的操作较多，请等当前操作完成。")
                db.execute("INSERT INTO jobs (id,fingerprint,payload,state,phase,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                           (payload["id"], fingerprint, encoded, "queued", "等待处理", now, now))
        self.event.set()
        return self.job(payload["id"])

    def update_job(self, job_id, state, phase, result=None, error=""):
        with self.database() as db:
            previous = db.execute("SELECT result FROM jobs WHERE id=?", (job_id,)).fetchone()
            saved = json.loads(previous[0]) if previous and previous[0] else {}
            merged = {key: saved[key] for key in ("english", "delivery") if key in saved}
            merged.update(result or {})
            db.execute("UPDATE jobs SET state=?,phase=?,result=?,error=?,updated_at=? WHERE id=?",
                       (state, phase, json.dumps(merged, ensure_ascii=False) if merged else None,
                        error, time.time(), job_id))

    def request_receipt(self, job_id):
        with self.lock:
            self.receipt_due.setdefault(job_id, 0)
        self.receipt_event.set()

    def reconcile_delivery(self, job_id):
        """Read the exact old ledger. Never translate, click, or resend a request."""
        if not self.receipt_lock.acquire(blocking=False):
            return False
        try:
            with self.database() as db:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "unknown":
                with self.lock:
                    self.receipt_due.pop(job_id, None)
                return False
            payload = json.loads(row["payload"])
            result = json.loads(row["result"]) if row["result"] else {}
            if payload["action"] not in {"send", "translate_send"}:
                with self.lock:
                    self.receipt_due.pop(job_id, None)
                return False
            identity = result.get("delivery")
            if not identity:
                # Older text-only jobs have enough data to reproduce the original
                # identity. An attachment batch must never be guessed.
                english = result.get("english") or payload.get("english")
                if payload.get("attachment_ids") or not english or not payload.get("expected_context"):
                    return False
                history_path = self.data_dir / "history" / (job_id + ".json")
                if not history_path.is_file():
                    return False
                config = json.loads(history_path.read_text(encoding="utf-8")).get("target", {})
                if not config.get("host"):
                    return False
                identity = {"fingerprint": core.delivery_fingerprint(core.validate_text(english), None,
                             payload["expected_context"], payload["auto_enter"]),
                            "host": config["host"], "transport": config.get("transport", "desktop")}
            fingerprint = identity.get("fingerprint", "")
            if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                return False
            client = self.ssh_factory({"host": identity["host"], "transport": identity.get("transport", "desktop")})
            receipt = client.action("delivery_status", request_id=job_id, fingerprint=fingerprint, timeout=20)
            if not (receipt.get("request_id") == job_id
                    and receipt.get("found") is True and receipt.get("matched") is True
                    and receipt.get("fingerprint") == fingerprint and receipt.get("completed") is True
                    and receipt.get("auto_enter") is payload["auto_enter"]):
                return False
            state = receipt.get("state")
            if state != "submitted" and not (state == "pasted" and payload["auto_enter"] is False):
                return False
            result.update(state=state, request_id=job_id, confirmed_by_receipt=True, delivery=identity)
            phase = "已通过 Ubuntu 回执确认提交" if state == "submitted" else "已通过 Ubuntu 回执确认载入草稿"
            with self.database() as db:
                changed = db.execute("UPDATE jobs SET state=?,phase=?,result=?,error='',updated_at=? WHERE id=? AND state='unknown'",
                                     (state, phase, json.dumps(result, ensure_ascii=False), time.time(), job_id)).rowcount
            if changed:
                try:
                    history_path = self.data_dir / "history" / (job_id + ".json")
                    if history_path.is_file():
                        history = json.loads(history_path.read_text(encoding="utf-8"))
                        history.update(state=state, detail=phase, delivery=identity)
                        core.atomic_json(history_path, history)
                    else:
                        core.history_record(payload["source"], result.get("english", payload.get("english", "")),
                                            {"host": identity["host"], "transport": identity.get("transport", "desktop")},
                                            job_id, state, phase, delivery=identity)
                except (OSError, ValueError):
                    pass  # The durable mobile receipt already confirms delivery.
                with self.lock:
                    self.receipt_due.pop(job_id, None)
            return bool(changed)
        except Exception:
            return False
        finally:
            with self.lock:
                if job_id in self.receipt_due:
                    self.receipt_due[job_id] = time.monotonic() + 15
            self.receipt_lock.release()

    def history(self):
        with self.database() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 50")]
        return [self.job(job_id) for job_id in ids]

    def public_state(self, csrf):
        config = self.config()
        with self.lock:
            self.last_seen = time.monotonic()
            target, reply, active = dict(self.target), dict(self.reply), self.active_job
        profiles = []
        for name, profile in config["profiles"].items():
            public = {"id": name, "label": profile.get("label", name), "model": profile.get("model", ""),
                      "kind": profile.get("kind", "api")}
            try:
                public.update(reasoning_effort=core.effective_reasoning_effort(profile),
                              translation_revision=translation_revision(profile))
            except core.BridgeError as exc:
                # Keep one invalid saved connection from hiding all other profiles.
                identity = [translation_revision({**profile, "reasoning_effort": ""}),
                            profile.get("reasoning_effort", "")]
                public.update(reasoning_effort="", configuration_error=str(exc), translation_revision=
                              hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest())
            profiles.append(public)
        return {"csrf": csrf, "profiles": profiles, "default_profile": config["provider"],
                "target": target, "reply": reply,
                "worker": {"online": bool(self.threads and self.threads[0].is_alive() and not self.worker_error),
                           "busy": active is not None},
                "active_job": self.job(active) if active else None}

    @staticmethod
    def safe_target(target):
        result = {key: target.get(key) for key in (
            "ready", "mode", "title", "context", "workspace", "reason", "busy", "window_visible", "new_session",
            "project_title", "session_title")}
        result["draft_chars"] = target.get("draft_chars", len(target.get("draft", "")))
        result["capabilities"] = {"native_attachments": (target.get("capabilities") or {}).get("native_attachments") is True}
        return result

    def projects(self, refresh=False):
        with self.lock:
            self.last_seen = time.monotonic()
            if refresh or not self.projects_initialized:
                self.projects_initialized = True
                self.project_catalog = {**self.project_catalog, "loading": True, "reason": "正在读取 Ubuntu 项目…"}
                self.projects_requested.set()
                self.capture_wake.set()
            return dict(self.project_catalog)

    @staticmethod
    def project_headers(catalog):
        """The first menu never includes another project's conversation rows."""
        result = {key: catalog.get(key) for key in ("mode", "context", "catalog_id", "active_project_id")}
        result.update(scope="projects", selected_project_id="", projects=[
            {**{key: project.get(key) for key in ("id", "title", "expanded", "selectable", "reason", "active")},
             "sessions": [], "sessions_loaded": False} for project in catalog.get("projects", [])])
        return result

    def fetch_projects(self):
        self.projects_requested.clear()
        try:
            with self.remote_lock:
                result = self.ssh_factory(self.config()["ssh"]).action("list_projects", timeout=60)
            with self.lock:
                self.project_catalog = {**self.project_headers(result), "loading": False, "reason": ""}
        except core.BridgeError as exc:
            with self.lock:
                self.project_catalog = {"projects": [], "loading": False, "reason": str(exc)}
        except Exception:
            with self.lock:
                self.project_catalog = {"projects": [], "loading": False, "reason": "项目读取未完成，请重试。"}

    def capture(self):
        if not self.remote_lock.acquire(blocking=False):
            return
        try:
            connection = dict(self.config()["ssh"])
            connection_key = json.dumps(connection, ensure_ascii=False, sort_keys=True)
            client = self.ssh_factory(connection)
            # The reply identifies its own page before/after reading. Do not put
            # a full composer/readiness scan in front of the lightweight read.
            reply = client.action("capture", timeout=45)
            context = reply.get("context")
            if not isinstance(context, str) or not re.fullmatch(r"[0-9a-f]{64}", context):
                raise core.BridgeError("回复所属会话尚未确认，请重新读取。")
            if connection != self.config()["ssh"]:
                raise core.BridgeError("Ubuntu 连接设置已变化，正在重新读取。")
            with self.lock:
                previous_generating = bool(self.reply.get("generating"))
                if self.target.get("context") != context or self.target_probe_connection != connection_key:
                    self.target = self.safe_target({"context": context, "mode": reply.get("mode"),
                        "title": reply.get("title", "当前会话"), "ready": False,
                        "reason": "最新回复已读取，正在确认输入状态…"})
                elif reply.get("generating"):
                    self.target = {**self.target, "ready": False, "busy": True, "reason": "Claude 正在回复"}
                if (self.project_catalog.get("context")
                        and self.project_catalog["context"] != context):
                    self.project_catalog = {"projects": [], "loading": False, "reason": ""}
                    self.projects_initialized = False
                self.accept_capture(reply, context)
                need_probe = (self.target_probe_context != context
                              or self.target_probe_connection != connection_key
                              or time.monotonic() - self.last_target_probe >= 30
                              or previous_generating and not reply.get("generating"))
            if not need_probe:
                return
            self.last_target_probe = time.monotonic()
            self.target_probe_context = context
            self.target_probe_connection = connection_key
            try:
                target = client.action("probe", timeout=30)
                safe_target = self.safe_target(target)
                if not isinstance(safe_target.get("context"), str) or not re.fullmatch(r"[0-9a-f]{64}", safe_target["context"]):
                    raise core.BridgeError("输入状态所属会话未确认。")
                if connection != self.config()["ssh"]:
                    raise core.BridgeError("Ubuntu 连接设置已变化，正在重新读取。")
                with self.lock:
                    if self.target.get("context") == safe_target.get("context"):
                        for key in ("project_title", "session_title"):
                            if self.target.get(key) and not safe_target.get(key):
                                safe_target[key] = self.target[key]
                    self.target = safe_target
                    if safe_target["context"] != context:
                        # A newer native page wins; never put the already read
                        # reply beneath the newly observed conversation title.
                        self.reply = {"text": "", "source_hash": "", "mode": safe_target.get("mode", ""),
                                      "context": safe_target["context"], "loading": True, "generating": False,
                                      "updated_at": 0, "error": "", "read_status": "loading"}
                        self.code_blocks = []
                        self.target_probe_context = ""
                        self.capture_wake.set()
            except Exception:
                with self.lock:
                    self.target = {**self.target, "ready": False,
                                   "reason": "最新回复已读取；输入状态暂未确认，正在重新连接。"}
            finally:
                self.last_target_probe = time.monotonic()
        except core.BridgeError as exc:
            self.capture_failed(str(exc))
        except Exception:
            self.capture_failed("暂时无法读取 Ubuntu 最新回复，正在重新连接。")
        finally:
            self.remote_lock.release()

    def capture_failed(self, message):
        with self.lock:
            self.target = {**self.target, "ready": False, "reason": message}
            self.reply = {"text": "", "source_hash": "", "mode": self.reply.get("mode", ""),
                          "context": self.target.get("context", ""), "loading": False, "generating": False,
                          "updated_at": 0, "error": message, "read_status": "error", "read_reason": message}
            self.code_blocks = []
            self.target_probe_context = ""

    def accept_capture(self, reply, expected_context):
        if not expected_context or reply.get("context") != expected_context:
            raise core.BridgeError("回复所属会话未确认，未显示其他项目的内容。")
        raw = reply.get("text", "").rstrip("\r\n")
        with self.lock:
            self.reply = {"text": raw, "source_hash": hashlib.sha256(raw.encode()).hexdigest() if raw else "",
                          "mode": reply.get("mode", ""), "context": expected_context, "loading": False,
                          "generating": bool(reply.get("generating")), "updated_at": time.time(), "error": "",
                          "read_status": reply.get("read_status") or ("ready" if raw else "empty"),
                          "read_reason": reply.get("read_reason", ""), "scope": "latest"}
            self.code_blocks = [value for value in reply.get("code_blocks", []) if isinstance(value, str)]

    def translate_reply(self, payload):
        with self.lock:
            reply, blocks = dict(self.reply), list(self.code_blocks)
        if not reply["text"] or reply["generating"] or payload["source_hash"] != reply["source_hash"]:
            raise core.BridgeError("回复已变化或仍在生成，等待完成后再翻译。")
        config = self.config()
        profile_id = payload["profile"]
        if profile_id not in config["profiles"]:
            raise core.BridgeError("翻译连接已变化，请重新选择。")
        profile = dict(config["profiles"][profile_id])
        revision = translation_revision(profile)
        key = translation_key(reply["text"], profile, profile_id, blocks)
        cached = self.reply_cache.get(key, reply["text"])
        if cached:
            return {"text": cached["text"], "source_hash": reply["source_hash"], "cached": True,
                    "translation_revision": revision}
        with self.database() as db:
            failed = db.execute("SELECT error FROM reply_attempts WHERE key=?", (key,)).fetchone()
        if failed and not payload["retry"]:
            raise core.BridgeError("上次翻译未完成。显示原文；需要重试时请重新打开自动翻译。")
        try:
            result = self.translator(profile_id, profile).translate_reply(reply["text"], code_blocks=blocks)
            self.reply_cache.put(key, reply["text"], result)
        except core.BridgeError as exc:
            with self.database() as db:
                db.execute("INSERT OR REPLACE INTO reply_attempts (key,error) VALUES (?,?)", (key, str(exc)))
            raise
        with self.database() as db:
            db.execute("DELETE FROM reply_attempts WHERE key=?", (key,))
        return {"text": result["text"], "source_hash": reply["source_hash"], "model": result.get("model", ""),
                "translation_revision": revision}

    def attachment_paths(self, identifiers):
        paths = []
        with self.database() as db:
            for identifier in identifiers:
                row = db.execute("SELECT path FROM uploads WHERE id=?", (identifier,)).fetchone()
                if row is None or not Path(row[0]).is_file():
                    raise core.BridgeError("附件已不可用，请重新选择。")
                paths.append(row[0])
        return paths

    def run_next_job(self):
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,payload FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return False
            db.execute("UPDATE jobs SET state='running',phase='正在处理',updated_at=? WHERE id=?", (time.time(), row["id"]))
        job_id, payload = row["id"], json.loads(row["payload"])
        with self.lock:
            self.active_job = job_id
        delivery_started = False
        receipts = []
        config = None
        english = payload["english"]
        delivery_info = None
        try:
            config = self.config()
            action = payload["action"]
            if action == "translate_reply":
                result = self.translate_reply(payload)
                self.update_job(job_id, "translated", "已翻译", result)
                return True
            if action == "translate":
                result = self.translator(payload["profile"]).translate(payload["source"])
                self.update_job(job_id, "translated", "已翻译", {**result, "english": result["text"]})
                return True
            with self.remote_lock:
                client = self.ssh_factory(config["ssh"])
                if action == "match_current":
                    with self.lock:
                        self.target = {"ready": False, "reason": "正在匹配 Ubuntu 当前对话…"}
                        self.reply = {"text": "", "source_hash": "", "mode": "Code", "context": "",
                                      "loading": True, "generating": False, "updated_at": 0}
                        self.code_blocks = []
                    result = client.action("match_current", timeout=90)
                    context = (result.get("target") or {}).get("context")
                    if (result.get("matched") is not True or not isinstance(context, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", context)
                            or result.get("context") != context):
                        raise core.BridgeError("当前对话身份尚未确认，请在 Ubuntu 打开要控制的 Code 会话后重试。")
                    target = self.safe_target({**result["target"],
                                               "project_title": result.get("project_title", ""),
                                               "session_title": result.get("session_title", "")})
                    result = {**result, "target": target}
                    with self.lock:
                        self.target = target
                        self.target_probe_connection = json.dumps(config["ssh"], ensure_ascii=False, sort_keys=True)
                        self.target_probe_context = ""
                        self.reply["context"] = context
                        self.project_catalog = {"projects": [], "loading": False, "reason": ""}
                        self.projects_initialized = False
                    self.update_job(job_id, "running", "已匹配，正在读取当前对话的最新回复", result)
                    try:
                        reply = client.action("capture", context=context, timeout=60)
                        self.accept_capture(reply, context)
                    except Exception as exc:
                        self.capture_failed(str(exc) if isinstance(exc, core.BridgeError) else "当前对话已匹配，但最新回复读取未完成，请刷新重试。")
                        result = {**result, "reply_loading": True}
                    self.update_job(job_id, "submitted", "已匹配当前对话", result)
                    return True
                if action in {"switch_project", "open_project"}:
                    navigating = action == "open_project" or bool(payload["session_id"])
                    if navigating:
                        with self.lock:
                            self.target = {**self.target, "ready": False, "reason": "正在读取所选项目的最近对话…"}
                            self.reply = {"text": "", "source_hash": "", "mode": "Code", "context": "",
                                          "loading": True, "generating": False, "updated_at": 0}
                            self.code_blocks = []
                    delivery_started = True
                    selection = {"context": payload["expected_context"], "catalog_id": payload["catalog_id"],
                                 "project_id": payload["project_id"]}
                    if action == "switch_project":
                        selection.update(session_id=payload["session_id"], session_catalog_id=payload.get("session_catalog_id", ""))
                    result = client.action(action, **selection, timeout=110)
                    context = (result.get("target") or {}).get("context")
                    read_reply = bool(context and (action == "open_project" or result.get("switched")))
                    with self.lock:
                        self.project_catalog = {**self.project_headers(result), "loading": False, "reason": ""}
                        self.projects_initialized = True
                        if read_reply:
                            self.reply = {"text": "", "source_hash": "", "mode": "Code", "context": context,
                                          "loading": True, "generating": False, "updated_at": 0}
                            self.code_blocks = []
                            self.target = self.safe_target(result["target"])
                            self.target_probe_connection = json.dumps(config["ssh"], ensure_ascii=False, sort_keys=True)
                            self.target_probe_context = ""
                    if read_reply:
                        self.update_job(job_id, "running", "正在读取所选项目的最近对话", result)
                        try:
                            reply = client.action("capture", context=context, timeout=60)
                            self.accept_capture(reply, context)
                        except Exception as exc:
                            self.capture_failed(str(exc) if isinstance(exc, core.BridgeError) else "已进入会话，但最新回复读取未完成，请刷新重试。")
                            result = {**result, "reply_loading": True}
                    phase = "已打开项目最近对话" if action == "open_project" else "已切换会话" if result.get("switched") else "已读取项目会话"
                    self.update_job(job_id, "submitted", phase, result)
                    self.capture_wake.set()
                    return True
                ready = client.action("check_ready", context=payload["expected_context"], timeout=90)
                if action == "new":
                    delivery_started = True
                    result = client.action("new", context=ready["context"])
                    self.update_job(job_id, "submitted", "已打开新会话", result)
                    return True
                if (payload["attachment_ids"] and ready.get("mode") == "Code"
                        and (ready.get("capabilities") or {}).get("native_attachments") is not True):
                    raise core.BridgeError("当前 Code 客户端的附件载入尚未就绪，请刷新连接状态后重试。")
                if action == "translate_send":
                    self.update_job(job_id, "running", "正在翻译")
                    result = self.translator(payload["profile"]).translate(payload["source"])
                    english = result["text"]
                    self.update_job(job_id, "running", "译文已生成，正在准备发送", {"english": english})
                paths = self.attachment_paths(payload["attachment_ids"])
                bundle = None
                if paths:
                    self.update_job(job_id, "running", "正在准备附件")
                    bundle = build_bundle(paths, True, data_dir=self.directory)
                    receipts = upload_bundle(client, bundle)
                text = core.validate_text(compose_prompt(english, receipts, desktop=True))
                delivery_info = {"fingerprint": core.delivery_fingerprint(text, bundle["batch_id"] if bundle else None,
                                                   ready["context"], payload["auto_enter"]),
                                 "host": config["ssh"]["host"], "transport": config["ssh"].get("transport", "desktop")}
                self.update_job(job_id, "running", "正在发送", {"english": english, "delivery": delivery_info})
                core.history_record(payload["source"], english, config["ssh"], job_id, "sending", attachments=receipts,
                                    delivery=delivery_info)
                delivery_started = True
                try:
                    result = client.action("send", text=text, request_id=job_id, enter=payload["auto_enter"],
                                           context=ready["context"], attachment_batch=bundle["batch_id"] if bundle else None,
                                           timeout=240 if bundle or ready.get("new_session") else 180)
                except core.BridgeError as exc:
                    if getattr(exc, "not_submitted", False):
                        delivery_started = False
                    raise
                state = result.get("state", "unknown")
                if state not in {"submitted", "pasted"}:
                    state = "unknown"
                phase = {"submitted": "已提交", "pasted": "已载入草稿", "unknown": "需要查看结果"}[state]
                history_warning = ""
                try:
                    core.history_record(payload["source"], english, config["ssh"], job_id, state, attachments=receipts,
                                        delivery=delivery_info)
                except OSError:
                    history_warning = "发送结果已确认，本机历史记录写入未完成。"
                self.update_job(job_id, state, phase,
                                {"english": english, "state": state, "request_id": job_id,
                                 **({"history_warning": history_warning} if history_warning else {})})
                if state == "unknown":
                    self.request_receipt(job_id)
            return True
        except core.BridgeError as exc:
            if getattr(exc, "not_submitted", False):
                delivery_started = False
            state = "unknown" if delivery_started else "failed"
            if config is not None and payload["action"] in {"send", "translate_send"}:
                try:
                    core.history_record(payload["source"], english, config["ssh"], job_id,
                                        "unconfirmed" if delivery_started else "not_sent", str(exc), attachments=receipts,
                                        delivery=delivery_info)
                except Exception:
                    pass  # The durable mobile receipt still records the uncertain outcome.
            self.update_job(job_id, state, "需要查看结果" if delivery_started else "操作未完成",
                            {"english": english} if english else None, str(exc))
            if delivery_started and payload["action"] in {"send", "translate_send"}:
                self.request_receipt(job_id)
            return True
        except Exception:
            self.update_job(job_id, "unknown" if delivery_started else "failed", "操作未完成",
                            {"english": english} if english else None,
                            error="后台操作未完成，请查看 Ubuntu 和发送记录。")
            if delivery_started and payload["action"] in {"send", "translate_send"}:
                self.request_receipt(job_id)
            return True
        finally:
            with self.lock:
                self.active_job = None
            if payload["action"] in {"open_project", "switch_project", "match_current"}:
                self.capture_wake.set()

    def start(self):
        if self.threads:
            return
        with self.database() as db:
            for row in db.execute("SELECT id FROM jobs WHERE state='unknown' ORDER BY created_at DESC LIMIT 50"):
                self.request_receipt(row[0])
        def jobs():
            while not self.stop_event.is_set():
                try:
                    worked = self.run_next_job()
                    self.worker_error = False
                    if not worked:
                        self.event.wait(1)
                        self.event.clear()
                except Exception:
                    self.worker_error = True
                    with self.lock:
                        self.active_job = None
                    try:
                        with self.database() as db:
                            db.execute("UPDATE jobs SET state='unknown',phase='需要查看结果',error=?,updated_at=? WHERE state='running'",
                                       ("后台记录暂时不可用，请先核对 Ubuntu；不会自动重发。", time.time()))
                    except Exception:
                        pass
                    self.stop_event.wait(3)

        def captures():
            while not self.stop_event.is_set():
                with self.lock:
                    active = self.active_job
                    recent = time.monotonic() - self.last_seen < 45
                if self.projects_requested.is_set() and active is None:
                    self.fetch_projects()
                elif recent and active is None:
                    self.capture()
                self.capture_wake.wait(5)
                self.capture_wake.clear()

        def receipts():
            while not self.stop_event.is_set():
                with self.lock:
                    due = [job_id for job_id, when in self.receipt_due.items() if when <= time.monotonic()]
                for job_id in due:
                    if self.stop_event.is_set():
                        break
                    self.reconcile_delivery(job_id)
                self.receipt_event.wait(3)
                self.receipt_event.clear()

        for function in (jobs, captures, receipts):
            thread = threading.Thread(target=function, daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stop_event.set()
        self.event.set()
        self.receipt_event.set()
        self.capture_wake.set()


def create_app(data_dir, public_origin, *, start_worker=True, bridge=None):
    origin = urlsplit(public_origin)
    if (origin.scheme not in {"https", "http"} or not origin.netloc or origin.path not in {"", "/"}
            or origin.username or origin.password or origin.query or origin.fragment):
        raise ValueError("public_origin 必须是完整的网页来源地址。")
    if origin.scheme != "https" and origin.hostname not in {"127.0.0.1", "localhost", "testserver"}:
        raise ValueError("公网入口必须使用 HTTPS。")
    public_origin = public_origin.rstrip("/")
    secure = origin.scheme == "https"
    core.DATA_DIR = Path(data_dir)
    service = bridge or MobileBridge(data_dir)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            service.start()
        yield
        service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestSizeLimit)
    app.state.bridge = service
    cookie_name = DEVICE_COOKIE

    def session(request, *, mutation=False):
        token = request.cookies.get(cookie_name, "")
        if not token or len(token) > 200:
            raise HTTPException(401, "当前浏览器未登记，请使用 Windows 提供的一次性配对码。")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with service.database() as db:
            row = db.execute("SELECT id,label,csrf,last_seen,expires_at,revoked FROM devices WHERE token_hash=?",
                             (token_hash,)).fetchone()
            now = time.time()
            if row is None or row["revoked"] or row["expires_at"] <= now:
                raise HTTPException(401, "设备凭证已撤销或过期，请在 Windows 重新登记。")
            if now - row["last_seen"] >= 60:
                db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, row["id"]))
        if mutation and (request.headers.get("origin") != public_origin or not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), row["csrf"])):
            raise HTTPException(403, "请求校验失败，请刷新页面后重试。")
        request.state.device = {"id": row["id"], "label": row["label"]}
        return row["csrf"]

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(core.BridgeError)
    async def bridge_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/api/login")
    def login(payload: LoginRequest, request: Request):
        if request.headers.get("origin") != public_origin:
            raise HTTPException(403, "请求来源不匹配。")
        now = time.monotonic()
        with service.lock:
            while service.login_failures and now - service.login_failures[0] > 300:
                service.login_failures.popleft()
            if len(service.login_failures) >= 8:
                raise HTTPException(429, "尝试次数较多，请五分钟后重试。")
            code_hash = hashlib.sha256(payload.code.encode()).hexdigest()
            token, csrf, device_id = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_hex(16)
            created = time.time()
            with service.database() as db:
                db.execute("BEGIN IMMEDIATE")
                pairing = db.execute("SELECT expires_at,consumed_at FROM device_pairings WHERE code_hash=?",
                                     (code_hash,)).fetchone()
                if pairing is None or pairing["consumed_at"] is not None or pairing["expires_at"] <= created:
                    service.login_failures.append(now)
                    raise HTTPException(401, "配对码无效、已使用或已过期，请在 Windows 生成新的配对码。")
                consumed = db.execute("UPDATE device_pairings SET consumed_at=?,device_id=? "
                                      "WHERE code_hash=? AND consumed_at IS NULL AND expires_at>?",
                                      (created, device_id, code_hash, created)).rowcount
                if consumed != 1:
                    service.login_failures.append(now)
                    raise HTTPException(401, "配对码已使用，请在 Windows 生成新的配对码。")
                db.execute("INSERT INTO devices(id,token_hash,csrf,label,created_at,last_seen,expires_at) VALUES (?,?,?,?,?,?,?)",
                           (device_id, hashlib.sha256(token.encode()).hexdigest(), csrf, payload.label,
                            created, created, created + DEVICE_TTL))
            service.login_failures.clear()
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(cookie_name, token, max_age=DEVICE_TTL, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        session(request, mutation=True)
        token = request.cookies[cookie_name]
        with service.database() as db:
            db.execute("UPDATE devices SET revoked=1 WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/state")
    def state(request: Request):
        csrf = session(request)
        return {**service.public_state(csrf), "device": request.state.device}

    @app.get("/api/history")
    def history(request: Request):
        session(request)
        return {"jobs": service.history()}

    @app.get("/api/projects")
    def projects(request: Request, refresh: bool = False):
        session(request)
        return service.projects(refresh)

    @app.post("/api/jobs")
    def jobs(payload: JobRequest, request: Request):
        session(request, mutation=True)
        return service.enqueue(payload)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, request: Request):
        session(request)
        if not ID_PATTERN.fullmatch(job_id):
            raise HTTPException(404, "未找到这次操作。")
        return service.job(job_id)

    @app.post("/api/uploads")
    async def upload(request: Request):
        session(request, mutation=True)
        with service.lock:
            if (service.target.get("mode") == "Code"
                    and (service.target.get("capabilities") or {}).get("native_attachments") is not True):
                raise HTTPException(400, "当前 Code 客户端的附件载入尚未就绪，请刷新连接状态后重试。")
        form = await request.form(max_files=1, max_fields=1)
        item = form.get("file")
        if item is None or not hasattr(item, "read"):
            raise HTTPException(400, "请选择一个文件。")
        identifier = secrets.token_hex(16)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(item.filename or "attachment").name)[:160]
        folder = service.directory / "uploads" / identifier
        folder.mkdir(parents=True, mode=0o700)
        path = folder / (name or "attachment")
        size = 0
        try:
            with path.open("xb") as out:
                while chunk := await item.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, "单个附件请控制在 256 MB 内。")
                    await asyncio.to_thread(out.write, chunk)
            file_kind = kind(path)
            with service.database() as db:
                db.execute("INSERT INTO uploads VALUES (?,?,?,?,?)", (identifier, str(path), path.name, file_kind, size))
        except Exception:
            path.unlink(missing_ok=True)
            folder.rmdir()
            raise
        finally:
            await item.close()
        return {"id": identifier, "name": path.name, "kind": file_kind, "size": size}

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/{asset}")
    def asset(asset: str):
        if asset not in {"app.css", "app.js", "manifest.webmanifest", "icon.svg", "sw.js"} or not (WEB / asset).is_file():
            raise HTTPException(404)
        return FileResponse(WEB / asset, headers={"Cache-Control": "no-cache"})

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--origin", default="http://127.0.0.1:18770")
    parser.add_argument("--port", type=int, default=18770)
    management = parser.add_mutually_exclusive_group()
    management.add_argument("--initialize-access", action="store_true", help="兼容旧命令，生成一次性设备配对码")
    management.add_argument("--pair-device", action="store_true", help="生成一次性设备配对码并写入本机文件")
    management.add_argument("--list-devices", action="store_true", help="列出已登记的浏览器设备")
    management.add_argument("--revoke-device", metavar="ID", help="撤销指定设备的访问凭证")
    args = parser.parse_args()
    if args.initialize_access or args.pair_device:
        if os.name != "nt":
            parser.error("请在 Windows 上生成设备配对码。")
        path = issue_pairing_code(args.data_dir)
        print(json.dumps({"configured": True, "auth_mode": "device_allowlist",
                          "pairing_information_file": str(path)}, ensure_ascii=False))
        return
    if args.list_devices:
        print(json.dumps(list_devices(args.data_dir), ensure_ascii=False))
        return
    if args.revoke_device:
        revoke_device(args.data_dir, args.revoke_device)
        print(json.dumps({"revoked": True, "device_id": args.revoke_device}, ensure_ascii=False))
        return
    import uvicorn
    with service_instance(args.data_dir):
        uvicorn.run(create_app(args.data_dir, args.origin), host="127.0.0.1", port=args.port,
                    access_log=False, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
