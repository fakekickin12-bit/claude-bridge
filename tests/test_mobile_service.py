"""Mobile backend regressions using temporary data and no external services."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import core
import mobile_service as mobile


ORIGIN = "https://bridge.example.test"
CONTEXT = "a" * 64
PROFILE = "synthetic-profile"
PASSWORD = "synthetic-login-password"
COOKIE = "__Host-bridge-device"


@pytest.fixture(autouse=True)
def synthetic_mobile_origin(monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_ORIGIN", ORIGIN)


class FakeRemote:
    def __init__(self):
        self.context = CONTEXT
        self.mode = "Chat"
        self.native_attachments = False
        self.receipts = {}
        self.calls = []
        self.send_error = None
        self.catalog = {"context": CONTEXT, "catalog_id": "b" * 64, "scope": "projects", "mode": "Code", "projects": [
            {"id": "c" * 64, "title": "Synthetic project", "expanded": False, "selectable": True,
             "sessions": []}]}
        self.reply = {"text": "Original reply", "mode": "Chat", "generating": False, "code_blocks": []}

    def client(self, config):
        assert config["host"] == "synthetic-host"
        return self

    def action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "probe":
            return {"ready": True, "mode": self.mode, "title": "Synthetic session", "context": self.context,
                    "busy": False, "window_visible": True, "draft": "Synthetic private draft",
                    "password": "synthetic-private-remote-field"}
        if action == "capture":
            if kwargs.get("context") and kwargs["context"] != self.context:
                raise core.BridgeError("Synthetic reply context changed")
            return {**self.reply, "context": self.context}
        if action == "check_ready":
            if kwargs["context"] != self.context:
                raise core.BridgeError("Synthetic stale target")
            return {"context": self.context, "ready": True, "mode": self.mode,
                    "capabilities": {"native_attachments": self.native_attachments}}
        if action == "send":
            if self.send_error:
                raise self.send_error
            assert kwargs["context"] == self.context
            return {"state": "submitted" if kwargs["enter"] else "pasted"}
        if action == "new":
            assert kwargs["context"] == self.context
            return {"opened": True}
        if action == "delivery_status":
            return self.receipts.get(kwargs["request_id"], {"found": False, "completed": False})
        if action == "list_projects":
            return dict(self.catalog)
        if action == "match_current":
            assert set(kwargs) == {"timeout"}
            return {"matched": True, "context": self.context, "project_title": "Current project",
                    "session_title": "Current task", "target": {"context": self.context, "title": "Current task",
                    "mode": "Code", "ready": False, "new_session": False,
                    "draft": "Never expose native draft", "password": "never-expose"}}
        if action in {"switch_project", "open_project"}:
            assert kwargs["context"] == self.context
            assert kwargs["catalog_id"] == self.catalog["catalog_id"]
            assert kwargs["project_id"] == "c" * 64
            switched = action == "open_project" or bool(kwargs["session_id"])
            if switched:
                self.context = "e" * 64
                self.mode = "Code"
                self.reply = {"text": "Latest reply in the selected project", "mode": "Code", "generating": False, "code_blocks": []}
            project = {**self.catalog["projects"][0], "expanded": True, "sessions_loaded": True, "sessions": [
                {"id": "d" * 64, "title": "Latest conversation", "selectable": True, "active": switched}]}
            return {**self.catalog, "context": self.context, "scope": "sessions", "selected_project_id": "c" * 64,
                    "session_catalog_id": "f" * 64, "projects": [project], "switched": switched, "expanded": not switched,
                    "target": {"context": self.context, "mode": "Code", "ready": True, "draft": "",
                               "capabilities": {"native_attachments": True}, "password": "never-expose"}}
        raise AssertionError(f"Unexpected remote action {action}")

    def count(self, action):
        return sum(name == action for name, _ in self.calls)


class FakeTranslator:
    def __init__(self):
        self.translations = []
        self.reply_translations = []
        self.reply_error = None

    def factory(self, profile_id, profile):
        assert profile_id == PROFILE
        assert profile["model"] == "synthetic-model"
        return self

    def translate(self, text):
        self.translations.append(text)
        return {"text": "Translated instruction", "model": "synthetic-model", "usage": {"total_tokens": 4}}

    def translate_reply(self, text, *, code_blocks):
        self.reply_translations.append((text, list(code_blocks)))
        if self.reply_error:
            raise self.reply_error
        return {"text": "回复译文", "model": "synthetic-model", "usage": {"total_tokens": 5}}


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    config = {"schema_version": 2, "provider": PROFILE,
              "profiles": {PROFILE: {"label": "Synthetic connection", "model": "synthetic-model", "kind": "api",
                                      "base_url": "https://api.example.test", "protocol": "responses",
                                      "key_encrypted": "synthetic-private-key", "key_env": "SYNTHETIC_PRIVATE_ENV",
                                      "codex_path": "synthetic-private-local-path"}},
              "ssh": {"host": "synthetic-host", "transport": "desktop"}}
    core.atomic_json(tmp_path / "config.json", config)
    remote, translator = FakeRemote(), FakeTranslator()
    service = mobile.MobileBridge(tmp_path, ssh_factory=remote.client, translator_factory=translator.factory)
    app = mobile.create_app(tmp_path, ORIGIN, start_worker=False, bridge=service)
    with TestClient(app, base_url=ORIGIN) as client:
        yield SimpleNamespace(service=service, remote=remote, translator=translator, client=client, app=app, directory=tmp_path)


def pairing_code(backend):
    note = mobile.issue_pairing_code(backend.directory)
    for line in note.read_text(encoding="utf-8").splitlines():
        if line.startswith("配对码："):
            return line.split("：", 1)[1].strip()
    raise AssertionError("The local pairing note did not contain a pairing-code line")


def login(backend):
    code = pairing_code(backend)
    response = backend.client.post("/api/login", headers={"Origin": ORIGIN}, json={"code": code, "label": "Synthetic phone"})
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"]}


def request(action="translate", **values):
    return mobile.JobRequest(id=uuid.uuid4().hex, action=action,
                             **({"source": "Synthetic instruction", "profile": PROFILE} | values))


def run(backend, payload):
    backend.service.enqueue(payload)
    assert backend.service.run_next_job()
    return backend.service.job(payload.id)


def reply_request(backend, **values):
    backend.service.capture()
    return request("translate_reply", source_hash=backend.service.reply["source_hash"], **values)


def test_authentication_origin_csrf_and_logout(backend):
    client = backend.client
    code = pairing_code(backend)
    payload = {"code": code, "label": "Synthetic phone"}
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json=payload).status_code == 403
    assert client.post("/api/login", headers={"Origin": "https://foreign.example.test"},
                       json=payload).status_code == 403
    assert client.post("/api/login", headers={"Origin": ORIGIN},
                       json={"code": "synthetic-invalid-code", "label": "Synthetic phone"}).status_code == 401
    response = client.post("/api/login", headers={"Origin": ORIGIN}, json=payload)
    assert response.status_code == 200
    headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"]}
    cookie = client.cookies.get(COOKIE)
    assert cookie
    with backend.service.database() as db:
        saved = db.execute("SELECT id,token_hash,csrf,expires_at FROM devices").fetchone()
    assert saved["token_hash"] == hashlib.sha256(cookie.encode()).hexdigest()
    if saved["token_hash"] == cookie:
        raise AssertionError("The device token was stored without hashing")
    assert set(response.json()) == {"csrf"}
    attributes = response.headers["Set-Cookie"]
    assert all(value in attributes for value in (COOKIE + "=", "Secure", "HttpOnly", "SameSite=strict", "Path=/"))
    assert "Max-Age=" + str(180 * 86400) in attributes
    assert time.time() + 179 * 86400 < saved["expires_at"] <= time.time() + 180 * 86400
    assert client.get("/api/state").status_code == 200
    payload = request().model_dump()
    for invalid_headers in ({}, {"Origin": ORIGIN}, {"X-CSRF-Token": headers["X-CSRF-Token"]},
                            {**headers, "Origin": "https://foreign.example.test"},
                            {**headers, "X-CSRF-Token": "incorrect"}):
        assert client.post("/api/jobs", headers=invalid_headers, json=payload).status_code == 403
    assert client.post("/api/jobs", headers=headers, json=payload).status_code == 200
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/state").status_code == 401
    with backend.service.database() as db:
        assert db.execute("SELECT revoked FROM devices WHERE id=?", (saved["id"],)).fetchone()[0] == 1
    client.cookies.set(COOKIE, cookie, domain="bridge.example.test", path="/")
    assert client.get("/api/state").status_code == 401


def test_expired_device_and_security_headers(backend):
    login(backend)
    with backend.service.database() as db:
        db.execute("UPDATE devices SET expires_at=?", (time.time() - 1,))
    response = backend.client.get("/api/state")
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_pairing_code_is_single_use_and_consumed_with_device_creation(backend):
    code = pairing_code(backend)
    assert len(code) >= 32
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    with backend.service.database() as db:
        issued = db.execute("SELECT * FROM device_pairings WHERE code_hash=?", (code_hash,)).fetchone()
    assert issued is not None and issued["consumed_at"] is None and issued["device_id"] is None
    assert 24 * 3600 - 2 <= issued["expires_at"] - issued["created_at"] <= 24 * 3600
    payload = {"code": code, "label": "Synthetic tablet"}
    assert backend.client.post("/api/login", headers={"Origin": ORIGIN}, json=payload).status_code == 200
    assert backend.client.post("/api/login", headers={"Origin": ORIGIN}, json=payload).status_code == 401
    with backend.service.database() as db:
        consumed = db.execute("SELECT * FROM device_pairings WHERE code_hash=?", (code_hash,)).fetchone()
        devices = db.execute("SELECT id FROM devices").fetchall()
    assert len(devices) == 1 and consumed["device_id"] == devices[0]["id"]
    assert consumed["consumed_at"] is not None


def test_concurrent_pairing_consumes_code_exactly_once(backend):
    code = pairing_code(backend)

    def enroll(index):
        # Independent service locks ensure the shared database enforces single
        # consumption even when requests arrive through separate app instances.
        service = mobile.MobileBridge(backend.directory, ssh_factory=backend.remote.client,
                                      translator_factory=backend.translator.factory)
        app = mobile.create_app(backend.directory, ORIGIN, start_worker=False, bridge=service)
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.post("/api/login", headers={"Origin": ORIGIN},
                                   json={"code": code, "label": "Synthetic device " + str(index)})
            return response.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(enroll, range(6)))
    assert statuses.count(200) == 1 and statuses.count(401) == 5
    with backend.service.database() as db:
        assert db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM device_pairings WHERE consumed_at IS NOT NULL").fetchone()[0] == 1


def test_expired_pairing_code_cannot_register_device(backend):
    code = pairing_code(backend)
    with backend.service.database() as db:
        db.execute("UPDATE device_pairings SET expires_at=?", (time.time() - 1,))
    response = backend.client.post("/api/login", headers={"Origin": ORIGIN},
                                   json={"code": code, "label": "Synthetic expired registration"})
    assert response.status_code == 401 and not backend.client.cookies.get(COOKIE)
    with backend.service.database() as db:
        assert db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
        assert db.execute("SELECT consumed_at FROM device_pairings").fetchone()[0] is None


def test_legacy_password_and_session_are_rejected(backend):
    salt = "12" * 16
    core.atomic_json(backend.service.directory / "access.json", {"salt": salt, "hash": "synthetic-legacy-hash"})
    response = backend.client.post("/api/login", headers={"Origin": ORIGIN}, json={"password": PASSWORD})
    assert response.status_code == 422
    token = "synthetic-legacy-session-token"
    with backend.service.database() as db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL)")
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), "legacy-csrf", time.time() + 86400))
    backend.client.cookies.set("__Host-bridge", token, domain="bridge.example.test", path="/")
    backend.client.cookies.set("bridge_session", token, domain="bridge.example.test", path="/")
    assert backend.client.get("/api/state").status_code == 401
    backend.client.cookies.set(COOKIE, token, domain="bridge.example.test", path="/")
    assert backend.client.get("/api/state").status_code == 401


def test_registered_device_authenticates_after_reload_and_server_restart(backend):
    headers = login(backend)
    token = backend.client.cookies.get(COOKIE)
    first = backend.client.get("/api/state")
    assert first.status_code == 200 and first.json()["csrf"] == headers["X-CSRF-Token"]
    assert backend.client.get("/api/state").status_code == 200
    restarted = mobile.MobileBridge(backend.directory, ssh_factory=backend.remote.client,
                                    translator_factory=backend.translator.factory)
    app = mobile.create_app(backend.directory, ORIGIN, start_worker=False, bridge=restarted)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set(COOKIE, token, domain="bridge.example.test", path="/")
        response = client.get("/api/state")
    assert response.status_code == 200 and response.json()["csrf"] == headers["X-CSRF-Token"]


def test_local_revocation_immediately_blocks_existing_device_cookie(backend):
    login(backend)
    devices = mobile.list_devices(backend.directory)
    assert len(devices) == 1 and devices[0]["label"] == "Synthetic phone"
    assert set(devices[0]) == {"id", "label", "created_at", "last_seen", "expires_at", "revoked"}
    mobile.revoke_device(backend.directory, devices[0]["id"])
    assert backend.client.get("/api/state").status_code == 401
    assert mobile.list_devices(backend.directory)[0]["revoked"]


def test_device_credentials_are_absent_from_json_and_stored_as_hashes(backend):
    code = pairing_code(backend)
    response = backend.client.post("/api/login", headers={"Origin": ORIGIN}, json={"code": code, "label": "Synthetic phone"})
    assert response.status_code == 200
    token = backend.client.cookies.get(COOKIE)
    with backend.service.database() as db:
        row = db.execute("SELECT token_hash FROM devices").fetchone()
        database_dump = "\n".join(db.iterdump())
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    responses = [response.json(), backend.client.get("/api/state").json(),
                 backend.client.get("/api/history").json(), mobile.list_devices(backend.directory)]
    for secret in (code, token):
        if secret in json.dumps(responses) or secret in database_dump:
            raise AssertionError("A raw device credential escaped its intended local note or cookie")


def test_public_state_exposes_only_profile_display_fields(backend):
    login(backend)
    backend.service.capture()
    response = backend.client.get("/api/state")
    assert response.status_code == 200
    state = response.json()
    profile = backend.service.config()["profiles"][PROFILE]
    assert state["profiles"] == [{"id": PROFILE, "label": "Synthetic connection", "model": "synthetic-model", "kind": "api",
                                  "reasoning_effort": "", "translation_revision": mobile.translation_revision(profile)}]
    serialized = json.dumps(state)
    for private in ("key_encrypted", "key_env", "base_url", "codex_path", "synthetic-private", "Synthetic private draft"):
        assert private not in serialized
    assert state["target"]["draft_chars"] == len("Synthetic private draft")


def test_invalid_saved_reasoning_does_not_hide_other_mobile_connections(backend):
    config = backend.service.config()
    config["profiles"]["invalid-profile"] = {**core.make_profile("OpenAI"), "model": "gpt-6.1-terra",
                                                "reasoning_effort": "ultra"}
    core.atomic_json(backend.directory / "config.json", config)
    state = backend.service.public_state("synthetic-csrf")
    assert len(state["profiles"]) == 2
    invalid = next(profile for profile in state["profiles"] if profile["id"] == "invalid-profile")
    assert invalid["configuration_error"] and len(invalid["translation_revision"]) == 64
    assert invalid["reasoning_effort"] == ""
    assert "key_encrypted" not in json.dumps(state["profiles"])


def test_project_list_is_authenticated_cached_and_explicitly_refreshed(backend):
    assert backend.client.get("/api/projects").status_code == 401
    login(backend)
    result = backend.client.get("/api/projects").json()
    assert result["loading"] is True
    assert backend.remote.count("list_projects") == 0
    assert backend.service.projects_requested.is_set()
    backend.service.fetch_projects()
    result = backend.client.get("/api/projects").json()
    assert result["loading"] is False and result["projects"][0]["title"] == "Synthetic project"
    assert backend.remote.count("list_projects") == 1
    assert not backend.service.projects_requested.is_set()
    assert backend.client.get("/api/projects?refresh=true").json()["loading"] is True
    assert backend.remote.count("list_projects") == 1
    assert backend.translator.translations == []


def test_initial_project_api_strips_all_cached_conversation_rows(backend):
    login(backend)
    backend.remote.catalog["projects"][0].update(sessions_loaded=True, sessions=[{"title": "Must not appear in the name menu"}])
    backend.service.projects()
    backend.service.fetch_projects()
    body = backend.client.get("/api/projects").json()
    assert body["scope"] == "projects" and body["selected_project_id"] == ""
    assert all(not item["sessions"] and item["sessions_loaded"] is False for item in body["projects"])
    assert "Must not appear" not in json.dumps(body)
    assert backend.remote.count("capture") == 0


def test_open_project_immediately_reads_latest_reply_in_confirmed_context(backend):
    backend.service.reply = {"text": "Previous project's reply", "source_hash": "f" * 64, "context": CONTEXT}
    payload = request("open_project", profile="", source="", expected_context=CONTEXT,
                      catalog_id="b" * 64, project_id="c" * 64)
    result = run(backend, payload)
    assert result["state"] == "submitted"
    assert result["result"]["scope"] == "sessions" and result["result"]["session_catalog_id"]
    assert result["result"]["projects"][0]["sessions"][0]["title"] == "Latest conversation"
    assert backend.service.projects()["projects"][0]["sessions"] == []
    assert backend.service.reply["text"] == "Latest reply in the selected project"
    assert backend.service.reply["context"] == backend.service.target["context"] == "e" * 64
    capture = next(values for name, values in backend.remote.calls if name == "capture")
    assert capture["context"] == "e" * 64
    assert backend.remote.count("send") == backend.remote.count("check_ready") == 0
    assert backend.translator.translations == [] and not (backend.directory / "history").exists()


def test_match_current_reads_open_task_without_ready_or_navigation(backend):
    backend.remote.context = "e" * 64
    backend.remote.reply.update(text="Current running task reply", mode="Code", generating=True)
    backend.service.reply = {"text": "Stale task reply", "context": CONTEXT}
    payload = mobile.JobRequest(id=uuid.uuid4().hex, action="match_current")
    result = run(backend, payload)
    assert result["state"] == "submitted" and result["result"]["matched"] is True
    assert result["result"]["target"]["project_title"] == "Current project"
    assert "draft" not in result["result"]["target"] and "password" not in result["result"]["target"]
    assert backend.service.reply["context"] == "e" * 64
    assert backend.service.reply["text"] == "Current running task reply" and backend.service.reply["generating"]
    assert [name for name, _ in backend.remote.calls] == ["match_current", "capture"]
    assert not backend.translator.translations and not backend.translator.reply_translations
    assert not backend.service.receipt_due and not (backend.directory / "history").exists()
    assert backend.service.capture_wake.is_set()
    backend.service.capture()
    assert backend.service.target["project_title"] == "Current project"


@pytest.mark.parametrize("extra", [{"source": "Draft"}, {"english": "English"}, {"attachment_ids": ["b" * 32]},
                                  {"expected_context": CONTEXT}, {"project_id": "c" * 64}])
def test_match_current_rejects_submission_data(backend, extra):
    with pytest.raises(HTTPException) as exc:
        backend.service.enqueue(mobile.JobRequest(id=uuid.uuid4().hex, action="match_current", **extra))
    assert exc.value.status_code == 400
    assert not backend.remote.calls


def test_match_timeout_is_retryable_read_failure_without_delivery_receipt(backend, monkeypatch):
    backend.service.reply = {"text": "Old reply", "context": CONTEXT}
    def fail(action, **kwargs):
        assert action == "match_current"
        raise core.BridgeError("Read timeout")
    monkeypatch.setattr(backend.remote, "action", fail)
    result = run(backend, mobile.JobRequest(id=uuid.uuid4().hex, action="match_current"))
    assert result["state"] == "failed"
    assert backend.service.reply["text"] == "" and backend.service.reply["loading"]
    assert not backend.service.receipt_due and backend.service.capture_wake.is_set()


def test_matched_task_cannot_show_reply_from_another_context(backend, monkeypatch):
    original = backend.remote.action
    def wrong_reply(action, **kwargs):
        if action == "capture":
            return {"context": "f" * 64, "text": "Wrong task", "mode": "Code"}
        return original(action, **kwargs)
    monkeypatch.setattr(backend.remote, "action", wrong_reply)
    result = run(backend, mobile.JobRequest(id=uuid.uuid4().hex, action="match_current"))
    assert result["state"] == "submitted" and result["result"]["reply_loading"]
    assert backend.service.reply["text"] == "" and backend.service.reply["context"] == CONTEXT


def test_navigation_timeout_clears_old_reply_and_does_not_poll_delivery_receipts(backend, monkeypatch):
    backend.service.reply = {"text": "Previous project's reply", "source_hash": "f" * 64, "context": CONTEXT}
    backend.service.target = {"context": CONTEXT, "ready": True}
    original = backend.remote.action

    def timeout(action, **values):
        if action == "open_project":
            raise core.BridgeError("Synthetic navigation timeout")
        return original(action, **values)

    monkeypatch.setattr(backend.remote, "action", timeout)
    payload = request("open_project", profile="", expected_context=CONTEXT, catalog_id="b" * 64, project_id="c" * 64)
    assert run(backend, payload)["state"] == "unknown"
    assert backend.service.reply["text"] == "" and backend.service.reply["loading"] is True
    assert backend.service.reply["updated_at"] == 0 and backend.service.target["ready"] is False
    assert payload.id not in backend.service.receipt_due
    assert backend.service.capture_wake.is_set()
    assert backend.remote.count("send") == backend.remote.count("delivery_status") == 0


def test_confirmed_navigation_never_displays_wrong_context_reply(backend, monkeypatch):
    original = backend.remote.action

    def wrong_capture(action, **values):
        if action == "capture":
            return {"context": CONTEXT, "text": "Wrong project content", "mode": "Code"}
        return original(action, **values)

    monkeypatch.setattr(backend.remote, "action", wrong_capture)
    payload = request("open_project", profile="", expected_context=CONTEXT, catalog_id="b" * 64, project_id="c" * 64)
    result = run(backend, payload)
    assert result["state"] == "submitted" and result["result"]["reply_loading"] is True
    assert backend.service.reply["text"] == "" and backend.service.reply["loading"] is False
    assert backend.service.reply["read_status"] == "error" and backend.service.reply["error"]
    assert backend.service.target["context"] == "e" * 64


def test_latest_reply_is_visible_before_readiness_probe_finishes(backend, monkeypatch):
    original = backend.remote.action
    observed = []

    def inspect(action, **kwargs):
        if action == "probe":
            observed.append(dict(backend.service.reply))
            assert backend.service.target["ready"] is False
        return original(action, **kwargs)

    monkeypatch.setattr(backend.remote, "action", inspect)
    backend.service.capture()
    assert [name for name, _ in backend.remote.calls] == ["capture", "probe"]
    assert observed[0]["text"] == "Original reply" and observed[0]["context"] == CONTEXT
    assert observed[0]["loading"] is False and observed[0]["scope"] == "latest"
    assert not backend.translator.translations and not backend.translator.reply_translations


def test_lightweight_poll_reuses_readiness_for_same_context(backend, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mobile.time, "monotonic", lambda: clock[0])
    backend.service.capture()
    clock[0] += 5
    backend.remote.reply["text"] = "Newest reply only"
    backend.service.capture()
    assert backend.remote.count("capture") == 2 and backend.remote.count("probe") == 1
    assert backend.service.reply["text"] == "Newest reply only"
    clock[0] += 30
    backend.service.capture()
    assert backend.remote.count("probe") == 2


def test_probe_timeout_does_not_hide_latest_reply_or_repeat_each_poll(backend, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mobile.time, "monotonic", lambda: clock[0])
    original = backend.remote.action
    probes = []

    def fail_probe(action, **kwargs):
        if action == "probe":
            probes.append(kwargs)
            clock[0] += 30
            raise core.BridgeError("Readiness timeout")
        return original(action, **kwargs)

    monkeypatch.setattr(backend.remote, "action", fail_probe)
    backend.service.capture()
    assert backend.service.reply["text"] == "Original reply" and not backend.service.reply["error"]
    assert backend.service.target["ready"] is False and "当前任务回复已读取" in backend.service.target["reason"]
    clock[0] += 5
    backend.service.capture()
    assert len(probes) == 1 and backend.remote.count("capture") == 2


def test_failed_latest_read_has_finite_error_instead_of_permanent_loading(backend, monkeypatch):
    backend.service.reply = {"text": "Old task", "context": CONTEXT, "loading": True}
    backend.service.target = {"context": CONTEXT, "ready": True}

    def fail(action, **kwargs):
        assert action == "capture"
        raise core.BridgeError("Latest reply unavailable")

    monkeypatch.setattr(backend.remote, "action", fail)
    backend.service.capture()
    assert backend.service.reply["text"] == "" and backend.service.reply["loading"] is False
    assert backend.service.reply["error"] == "Latest reply unavailable"
    assert backend.service.reply["read_status"] == "error"
    assert backend.service.target["ready"] is False and not backend.service.remote_lock.locked()


def test_newer_probe_page_never_gets_previous_page_reply(backend, monkeypatch):
    original = backend.remote.action

    def changed_page(action, **kwargs):
        if action == "probe":
            backend.remote.context = "e" * 64
        return original(action, **kwargs)

    monkeypatch.setattr(backend.remote, "action", changed_page)
    backend.service.capture()
    assert backend.service.target["context"] == backend.service.reply["context"] == "e" * 64
    assert backend.service.reply["text"] == "" and backend.service.reply["loading"] is True
    assert backend.service.capture_wake.is_set()
    backend.remote.reply["text"] = "New page's reply"
    backend.service.capture()
    assert backend.service.reply["text"] == "New page's reply" and not backend.service.reply["loading"]


def test_finished_generation_rechecks_readiness_without_guessing(backend):
    backend.remote.reply["generating"] = True
    backend.service.capture()
    backend.service.capture()
    assert backend.service.target["ready"] is False and backend.remote.count("probe") == 1
    backend.remote.reply["generating"] = False
    backend.service.capture()
    assert backend.remote.count("probe") == 2 and backend.service.target["ready"] is True


def test_old_job_payload_without_new_session_signature_keeps_original_idempotency(backend):
    payload = request()
    old = payload.model_dump()
    old.pop("session_catalog_id")
    old.pop("incremental")
    old.pop("connection_revision")
    encoded = json.dumps(old, sort_keys=True, ensure_ascii=False)
    with backend.service.database() as db:
        db.execute("INSERT INTO jobs(id,fingerprint,payload,state,phase,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (payload.id, hashlib.sha256(encoded.encode()).hexdigest(), encoded, "queued", "Waiting", time.time(), time.time()))
    assert backend.service.enqueue(payload)["state"] == "queued"
    assert backend.service.run_next_job() and not backend.service.run_next_job()
    assert backend.translator.translations == [payload.source]


@pytest.mark.parametrize("selected", [False, True])
def test_project_navigation_never_translates_sends_or_writes_prompt_history(backend, selected):
    backend.service.reply = {"text": "Old reply", "source_hash": "f" * 64}
    backend.service.code_blocks = ["old code"]
    payload = request("switch_project", profile="", expected_context=CONTEXT,
                      catalog_id="b" * 64, project_id="c" * 64, session_id="d" * 64 if selected else "",
                      session_catalog_id="f" * 64 if selected else "")
    result = run(backend, payload)
    assert result["state"] == "submitted"
    assert backend.remote.count("switch_project") == 1
    assert backend.remote.count("check_ready") == 0
    assert backend.remote.count("send") == 0
    assert backend.translator.translations == []
    assert not (backend.directory / "history").exists()
    assert backend.service.project_catalog["loading"] is False
    if selected:
        assert backend.service.reply["text"] == "Latest reply in the selected project" and backend.service.code_blocks == []
        assert backend.service.reply["context"] == backend.service.target["context"]
        assert backend.service.target["context"] == "e" * 64
        assert backend.service.target["capabilities"]["native_attachments"] is True
        assert "password" not in backend.service.target
    else:
        assert backend.service.reply["text"] == "Old reply"
    assert backend.service.enqueue(payload)["state"] == "submitted"
    assert not backend.service.run_next_job()
    assert backend.remote.count("switch_project") == 1


@pytest.mark.parametrize("changes", [{"expected_context": ""}, {"catalog_id": ""}, {"project_id": "wrong"},
                                      {"session_id": "wrong"}, {"attachment_ids": ["d" * 32]}])
def test_project_navigation_validates_identity_before_remote_work(backend, changes):
    payload = request("switch_project", expected_context=CONTEXT, catalog_id="b" * 64,
                      project_id="c" * 64).model_copy(update=changes)
    with pytest.raises(HTTPException):
        backend.service.enqueue(payload)
    assert backend.remote.calls == []


def test_same_job_and_payload_run_once_even_with_concurrent_enqueue(backend):
    payload = request()
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda _: backend.service.enqueue(payload), range(4)))
    assert {job["id"] for job in jobs} == {payload.id}
    assert backend.service.run_next_job()
    assert not backend.service.run_next_job()
    assert backend.translator.translations == [payload.source]
    assert backend.service.enqueue(payload)["state"] == "translated"
    assert not backend.service.run_next_job()


def test_same_id_with_different_payload_is_http_conflict(backend):
    headers = login(backend)
    payload = request()
    assert backend.client.post("/api/jobs", headers=headers, json=payload.model_dump()).status_code == 200
    changed = payload.model_copy(update={"source": "Different instruction"})
    response = backend.client.post("/api/jobs", headers=headers, json=changed.model_dump())
    assert response.status_code == 409
    assert backend.service.job(payload.id)["source"] == payload.source


@pytest.mark.parametrize("action", ["send", "translate_send", "new"])
def test_stale_target_blocks_sending_and_translation(backend, action):
    payload = request(action, english="English instruction", expected_context="b" * 64)
    result = run(backend, payload)
    assert result["state"] == "failed"
    assert backend.remote.count("check_ready") == 1
    assert backend.remote.count("send") == backend.remote.count("new") == 0
    assert backend.translator.translations == []


def test_successful_translate_send_preserves_context_and_draft_mode(backend):
    payload = request("translate_send", expected_context=CONTEXT, auto_enter=False)
    result = run(backend, payload)
    assert result["state"] == "pasted"
    send = next(values for action, values in backend.remote.calls if action == "send")
    assert send["context"] == CONTEXT and send["request_id"] == payload.id
    assert send["text"] == "Translated instruction" and send["enter"] is False
    assert backend.translator.translations == [payload.source]


def test_unknown_send_result_is_retained_and_never_retried(backend):
    backend.remote.send_error = core.BridgeError("Synthetic connection loss")
    payload = request("send", english="Instruction", expected_context=CONTEXT)
    result = run(backend, payload)
    assert result["state"] == "unknown"
    assert backend.remote.count("send") == 1
    assert backend.service.enqueue(payload)["state"] == "unknown"
    assert not backend.service.run_next_job()
    restarted = mobile.MobileBridge(backend.directory, ssh_factory=backend.remote.client,
                                    translator_factory=backend.translator.factory)
    assert restarted.job(payload.id)["state"] == "unknown"
    assert not restarted.run_next_job()
    assert backend.remote.count("send") == 1


def stored_delivery(backend, job_id):
    with backend.service.database() as db:
        return json.loads(db.execute("SELECT result FROM jobs WHERE id=?", (job_id,)).fetchone()[0])["delivery"]


def confirmed_receipt(backend, payload, *, state="submitted", auto_enter=True):
    return {"found": True, "matched": True, "completed": state == "submitted" or state == "pasted" and auto_enter is False,
            "request_id": payload.id, "fingerprint": stored_delivery(backend, payload.id)["fingerprint"],
            "state": state, "auto_enter": auto_enter, "context": CONTEXT}


def test_late_receipt_recovers_original_translation_once_without_resending(backend):
    backend.remote.send_error = core.BridgeError("Synthetic local send timeout")
    payload = request("translate_send", expected_context=CONTEXT)
    before = run(backend, payload)
    assert before["state"] == "unknown" and before["english"] == "Translated instruction"
    assert "delivery" not in before["result"]
    backend.remote.receipts[payload.id] = confirmed_receipt(backend, payload)
    assert backend.service.reconcile_delivery(payload.id)
    after = backend.service.job(payload.id)
    assert after["state"] == "submitted" and after["result"]["confirmed_by_receipt"] is True
    assert after["error"] == "" and after["english"] == "Translated instruction"
    assert backend.service.enqueue(payload)["state"] == "submitted"
    assert not backend.service.run_next_job()
    assert backend.remote.count("send") == backend.remote.count("delivery_status") == 1
    assert backend.translator.translations == [payload.source]


@pytest.mark.parametrize("state,auto_enter,changes", [
    ("pasted", True, {}), ("preparing", True, {}), ("submitting", True, {}),
    ("submitted", True, {"fingerprint": "0" * 64}), ("submitted", True, {"found": False}),
    ("submitted", True, {"auto_enter": "true"}), ("submitted", True, {"request_id": "0" * 32})])
def test_receipt_intermediate_missing_mismatched_or_untyped_stays_unknown(backend, state, auto_enter, changes):
    backend.remote.send_error = core.BridgeError("Synthetic local timeout")
    payload = request("send", english="Instruction", expected_context=CONTEXT)
    assert run(backend, payload)["state"] == "unknown"
    backend.remote.receipts[payload.id] = confirmed_receipt(backend, payload, state=state, auto_enter=auto_enter) | changes
    assert not backend.service.reconcile_delivery(payload.id)
    assert backend.service.job(payload.id)["state"] == "unknown"
    assert backend.remote.count("send") == 1


def test_draft_only_receipt_and_restarted_worker_use_original_target(backend):
    backend.remote.send_error = core.BridgeError("Synthetic timeout")
    payload = request("send", english="Instruction", expected_context=CONTEXT, auto_enter=False)
    assert run(backend, payload)["state"] == "unknown"
    backend.remote.receipts[payload.id] = confirmed_receipt(backend, payload, state="pasted", auto_enter=False)
    config = backend.service.config()
    config["ssh"]["host"] = "new-target-must-not-receive-old-receipt-query"
    core.atomic_json(backend.directory / "config.json", config)
    restarted = mobile.MobileBridge(backend.directory, ssh_factory=backend.remote.client,
                                    translator_factory=backend.translator.factory)
    assert restarted.reconcile_delivery(payload.id)
    assert restarted.job(payload.id)["state"] == "pasted"
    assert backend.remote.count("send") == 1


def test_legacy_text_receipt_uses_saved_target_but_never_guesses_attachment_batch(backend):
    backend.remote.send_error = core.BridgeError("Synthetic timeout")
    payload = request("send", english="Instruction", expected_context=CONTEXT)
    assert run(backend, payload)["state"] == "unknown"
    receipt = confirmed_receipt(backend, payload)
    with backend.service.database() as db:
        db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps({"english": payload.english}), payload.id))
    backend.remote.receipts[payload.id] = receipt
    assert backend.service.reconcile_delivery(payload.id)
    assert backend.service.job(payload.id)["state"] == "submitted"


def test_unknown_generic_error_retains_english_and_exact_delivery_identity(backend, monkeypatch):
    backend.remote.send_error = RuntimeError("Synthetic unexpected process error")
    payload = request("translate_send", expected_context=CONTEXT)
    result = run(backend, payload)
    assert result["state"] == "unknown" and result["english"] == "Translated instruction"
    assert stored_delivery(backend, payload.id)["fingerprint"]
    assert backend.remote.count("send") == 1


def test_unrecognized_delivery_state_does_not_claim_draft_loaded(backend, monkeypatch):
    action = backend.remote.action

    def uncertain_result(action_name, **kwargs):
        result = action(action_name, **kwargs)
        return {"state": "submitting"} if action_name == "send" else result

    monkeypatch.setattr(backend.remote, "action", uncertain_result)
    payload = request("send", english="Instruction", expected_context=CONTEXT)
    result = run(backend, payload)
    assert result["state"] == "unknown"
    assert result["phase"] == "需要查看结果"
    assert backend.service.enqueue(payload)["state"] == "unknown"
    assert not backend.service.run_next_job()
    assert backend.remote.count("send") == 1


def test_running_job_becomes_unknown_after_restart_without_replay(backend):
    payload = request("send", english="Instruction", expected_context=CONTEXT)
    backend.service.enqueue(payload)
    backend.service.update_job(payload.id, "running", "Synthetic interrupted operation")
    restarted = mobile.MobileBridge(backend.directory, ssh_factory=backend.remote.client,
                                    translator_factory=backend.translator.factory)
    assert restarted.job(payload.id)["state"] == "unknown"
    assert not restarted.run_next_job()
    assert backend.remote.count("send") == 0


def test_new_session_timeout_is_unknown_and_not_replayed(backend, monkeypatch):
    action = backend.remote.action

    def timed_out(action_name, **kwargs):
        if action_name == "new":
            backend.remote.calls.append((action_name, kwargs))
            raise core.BridgeError("Synthetic new session timeout")
        return action(action_name, **kwargs)

    monkeypatch.setattr(backend.remote, "action", timed_out)
    payload = request("new", expected_context=CONTEXT)
    result = run(backend, payload)
    assert result["state"] == "unknown"
    assert backend.remote.count("new") == 1
    assert backend.service.enqueue(payload)["state"] == "unknown"
    assert not backend.service.run_next_job()
    assert backend.remote.count("new") == 1


def test_known_not_submitted_error_is_a_failure(backend):
    error = core.BridgeError("Synthetic draft focus failure")
    error.not_submitted = True
    backend.remote.send_error = error
    payload = request("send", english="Instruction", expected_context=CONTEXT)
    assert run(backend, payload)["state"] == "failed"


@pytest.mark.parametrize("connection_lost", [False, True])
def test_history_write_failure_preserves_uncertain_receipt_and_queue(backend, monkeypatch, connection_lost):
    writes = []

    def history(*args, **kwargs):
        writes.append(args)
        if len(writes) > 1:
            raise OSError("Synthetic desktop history unavailable")

    monkeypatch.setattr(core, "history_record", history)
    if connection_lost:
        backend.remote.send_error = core.BridgeError("Synthetic connection loss")
    delivery = request("send", english="Instruction", expected_context=CONTEXT)
    result = run(backend, delivery)
    expected_state = "unknown" if connection_lost else "submitted"
    assert result["state"] == expected_state
    assert backend.service.active_job is None
    assert backend.service.enqueue(delivery)["state"] == expected_state
    following = request()
    assert run(backend, following)["state"] == "translated"
    assert backend.remote.count("send") == 1
    assert backend.translator.translations == [following.source]


def test_reply_translation_reuses_cache_across_jobs(backend):
    backend.remote.reply["code_blocks"] = ["git status\ngit diff"]
    first = reply_request(backend)
    result = run(backend, first)
    assert result["state"] == "translated" and result["result"]["text"] == "回复译文"
    second = request("translate_reply", source_hash=first.source_hash)
    result = run(backend, second)
    assert result["state"] == "translated" and result["result"]["cached"] is True
    assert backend.translator.reply_translations == [("Original reply", ["git status\ngit diff"])]


def test_reply_translation_uses_one_profile_snapshot_for_call_and_cache(backend, monkeypatch):
    backend.service.capture()
    config = backend.service.config()
    profile = config["profiles"][PROFILE]
    profile.update(model="gpt-6.1-terra", reasoning_effort="low")
    core.atomic_json(backend.directory / "config.json", config)
    original_config = backend.service.config
    observed = []

    def saving_configuration_during_read():
        snapshot = original_config()
        newer = json.loads(json.dumps(snapshot))
        newer["profiles"][PROFILE]["reasoning_effort"] = "high"
        core.atomic_json(backend.directory / "config.json", newer)
        return snapshot

    def factory(profile_id, captured_profile):
        assert profile_id == PROFILE
        observed.append(captured_profile["reasoning_effort"])
        return backend.translator

    monkeypatch.setattr(backend.service, "config", saving_configuration_during_read)
    backend.service.translator_factory = factory
    result = backend.service.translate_reply({"profile": PROFILE, "source_hash": backend.service.reply["source_hash"], "retry": False})
    assert observed == ["low"]
    assert result["translation_revision"] == mobile.translation_revision(profile)
    low_key = mobile.translation_key("Original reply", profile, PROFILE)
    high_key = mobile.translation_key("Original reply", {**profile, "reasoning_effort": "high"}, PROFILE)
    assert backend.service.reply_cache.get(low_key, "Original reply")
    assert backend.service.reply_cache.get(high_key, "Original reply") is None


def test_failed_reply_translation_requires_explicit_retry(backend):
    backend.translator.reply_error = core.BridgeError("Synthetic translation outage")
    first = reply_request(backend)
    assert run(backend, first)["state"] == "failed"
    backend.translator.reply_error = None
    second = request("translate_reply", source_hash=first.source_hash)
    assert run(backend, second)["state"] == "failed"
    assert len(backend.translator.reply_translations) == 1
    third = request("translate_reply", source_hash=first.source_hash, retry=True)
    assert run(backend, third)["state"] == "translated"
    assert len(backend.translator.reply_translations) == 2
    fourth = request("translate_reply", source_hash=first.source_hash)
    assert run(backend, fourth)["result"]["cached"] is True
    assert len(backend.translator.reply_translations) == 2


@pytest.mark.parametrize("generating,source_hash", [(True, None), (False, "c" * 64)])
def test_reply_translation_rejects_partial_or_stale_reply(backend, generating, source_hash):
    payload = reply_request(backend)
    backend.service.reply["generating"] = generating
    if source_hash is not None:
        payload = payload.model_copy(update={"source_hash": source_hash})
    assert run(backend, payload)["state"] == "failed"
    assert backend.translator.reply_translations == []


def live_reply(backend, text, generating=True, context=CONTEXT):
    backend.service.target = {"context": context, "ready": True, "mode": "Code"}
    backend.service.accept_capture({"context": context, "text": text, "generating": generating, "mode": "Code"}, context)
    connection = backend.service.public_state("synthetic-csrf")["connection_revision"]
    return request("translate_reply", source_hash=backend.service.reply["source_hash"], incremental=True,
                   expected_context=context, connection_revision=connection)


def test_live_reply_translates_new_sentences_during_generation_and_flushes_tail(backend):
    first = "First complete sentence for translation.\n\n"
    tail = "Unfinished tail"
    first_job = run(backend, live_reply(backend, first + tail))
    value = first_job["result"]
    assert first_job["state"] == "translated" and value["incremental"]
    assert value["translated_source"] == first
    assert value["text"] == "回复译文\n\n" + tail
    assert value["generating"] and not value["next_ready"] and not value["complete"]
    before = list(backend.translator.reply_translations)
    repeated = run(backend, live_reply(backend, first + tail))
    assert repeated["result"]["translated_source"] == first
    assert backend.translator.reply_translations == before
    final = run(backend, live_reply(backend, first + tail, generating=False))
    assert final["result"]["complete"]
    assert backend.translator.reply_translations == [(first.strip(), []), (tail, [])]
    assert backend.remote.count("send") == 0


def test_live_reply_assembled_cache_survives_restart_with_different_chunk_boundaries(backend):
    first = "An early completed sentence was translated while the reply grew.\n\n"
    tail = "The later tail arrives without a final punctuation mark"
    initial = run(backend, live_reply(backend, first + "The later tail"))
    assert initial["result"]["translated_source"] == first
    finished_payload = live_reply(backend, first + tail, generating=False)
    finished = run(backend, finished_payload)
    assert finished["result"]["complete"]
    assert [item[0] for item in backend.translator.reply_translations] == [first.strip(), tail]
    restarted = mobile.MobileBridge(backend.directory, ssh_factory=backend.remote.client,
                                   translator_factory=backend.translator.factory)
    restarted.target = dict(backend.service.target)
    restarted.reply = dict(backend.service.reply)
    restarted.code_blocks = list(backend.service.code_blocks)
    restored = restarted.translate_reply(finished_payload.model_dump())
    assert restored["complete"] and restored["translated_text"] == finished["result"]["translated_text"]
    assert len(backend.translator.reply_translations) == 2, "The full assembled disk cache avoids paying for a differently sized final chunk"
    assert backend.remote.count("send") == 0


def test_live_reply_accepts_growth_while_chunk_is_running_without_repeating_prefix(backend):
    first = "One completed sentence is ready for translation.\n\n"
    appended = "Another completed sentence arrives while translating."
    payload = live_reply(backend, first)
    original = backend.translator.translate_reply
    def growing(text, *, code_blocks):
        live_reply(backend, first + appended)
        return original(text, code_blocks=code_blocks)
    backend.translator.translate_reply = growing
    result = run(backend, payload)
    assert result["state"] == "translated"
    assert result["result"]["translated_source"] == first.rstrip("\r\n")
    assert result["result"]["source"] == first + appended
    assert result["result"]["next_ready"]
    backend.translator.translate_reply = original
    following = run(backend, live_reply(backend, first + appended, generating=False))
    assert following["result"]["complete"]
    assert [item[0] for item in backend.translator.reply_translations] == [first.strip(), appended]


@pytest.mark.parametrize("change", ["context", "rewrite", "connection"])
def test_live_reply_rejects_late_wrong_page_or_rewritten_chunk(backend, change):
    first = "A sufficiently long original sentence is ready."
    payload = live_reply(backend, first)
    original = backend.translator.translate_reply
    def changed(text, *, code_blocks):
        if change == "context":
            live_reply(backend, "Different project reply.", context="b" * 64)
        elif change == "rewrite":
            live_reply(backend, "This native message was rewritten completely.")
        else:
            config = backend.service.config()
            config["ssh"]["host"] = "another-synthetic-host"
            core.atomic_json(backend.directory / "config.json", config)
        return original(text, code_blocks=code_blocks)
    backend.translator.translate_reply = changed
    result = run(backend, payload)
    assert result["state"] == "failed" and result["result"] is None
    assert backend.remote.count("send") == 0


def test_live_reply_failure_does_not_retry_growing_suffix_but_new_body_can_translate(backend):
    first = "A complete sentence ready for live translation."
    backend.translator.reply_error = core.BridgeError("Synthetic incremental outage")
    assert run(backend, live_reply(backend, first))["state"] == "failed"
    backend.translator.reply_error = None
    assert run(backend, live_reply(backend, first + " More native content arrives."))["state"] == "failed"
    assert len(backend.translator.reply_translations) == 1
    replacement = run(backend, live_reply(backend, "A completely different new reply can translate."))
    assert replacement["state"] == "translated"
    assert len(backend.translator.reply_translations) == 2


def test_live_reply_failure_can_retry_explicitly_and_chunk_cache_survives_restart(backend):
    text = "Another complete sentence ready for live translation."
    payload = live_reply(backend, text)
    backend.translator.reply_error = core.BridgeError("Synthetic incremental outage")
    assert run(backend, payload)["state"] == "failed"
    backend.translator.reply_error = None
    retry = live_reply(backend, text).model_copy(update={"retry": True})
    assert run(backend, retry)["result"]["complete"]
    backend.service.reply_streams.clear()
    assert run(backend, live_reply(backend, text))["result"]["complete"]
    assert len(backend.translator.reply_translations) == 2


def test_live_reply_failure_is_not_retried_when_only_unprocessed_tail_is_rewritten(backend):
    first = "A completed sentence ready for a failed translation.\n\n"
    backend.translator.reply_error = core.BridgeError("Synthetic incremental outage")
    assert run(backend, live_reply(backend, first + "Original unfinished tail"))["state"] == "failed"
    backend.translator.reply_error = None
    assert run(backend, live_reply(backend, first + "Rewritten unfinished tail"))["state"] == "failed"
    assert len(backend.translator.reply_translations) == 1


def test_live_reply_unfinished_code_waits_without_model_call(backend):
    result = run(backend, live_reply(backend, "```python\nprint('unfinished')"))
    assert result["state"] == "translated" and not result["result"]["next_ready"]
    assert result["result"]["translated_source"] == ""
    assert backend.translator.reply_translations == []


def test_live_reply_wrong_connection_is_rejected_before_paid_call(backend):
    payload = live_reply(backend, "A complete sentence ready for live translation.")
    result = run(backend, payload.model_copy(update={"connection_revision": "c" * 64}))
    assert result["state"] == "failed"
    assert backend.translator.reply_translations == []


@pytest.mark.parametrize("scope,count,expected_scope,expected_count", [
    ("current_turn", 3, "current_turn", 3), ("current_turn", True, "current_turn", 1),
    ("current_turn", -1, "current_turn", 1), ("current_turn", "3", "current_turn", 1),
    ("current_turn", 10001, "current_turn", 1), ("latest", 3, "latest", 1),
    ("untrusted_scope", 3, "latest", 1),
])
def test_current_task_capture_scope_and_count_are_bounded(backend, scope, count, expected_scope, expected_count):
    backend.service.accept_capture({"context": CONTEXT, "text": "Synthetic current task reply",
        "scope": scope, "message_count": count}, CONTEXT)
    assert backend.service.reply["scope"] == expected_scope
    assert backend.service.reply["message_count"] == expected_count


def test_cumulative_current_task_progress_retains_prior_messages_and_translations(backend):
    messages = ["Initial task progress is available for translation.",
                "A second progress message arrives for this same task.",
                "A third final result completes the current task."]
    config = backend.service.public_state("synthetic-csrf")
    backend.service.target = {"context": CONTEXT, "ready": True, "mode": "Code"}
    prior_translated = ""
    for count in range(1, 4):
        source = "\n\n".join(messages[:count])
        backend.service.accept_capture({"context": CONTEXT, "text": source, "generating": count < 3,
            "mode": "Code", "scope": "current_turn", "message_count": count}, CONTEXT)
        assert backend.service.reply["text"] == source
        assert backend.service.reply["scope"] == "current_turn" and backend.service.reply["message_count"] == count
        result = run(backend, request("translate_reply", source_hash=backend.service.reply["source_hash"],
            incremental=True, expected_context=CONTEXT, connection_revision=config["connection_revision"]))["result"]
        assert result["translated_source"] == source and result["complete"]
        assert result["translated_text"].startswith(prior_translated)
        prior_translated = result["translated_text"]
    assert [item[0] for item in backend.translator.reply_translations] == messages
    assert backend.remote.count("send") == 0


def test_new_user_turn_in_same_context_rejects_in_flight_previous_task_translation(backend):
    first = "Previous task progress should not become the new task reply."
    current = "A new user task in the same native conversation has its own progress."
    payload = live_reply(backend, first)
    backend.service.reply["turn_id"] = "1" * 64
    original = backend.translator.translate_reply
    def newer_turn(text, *, code_blocks):
        backend.service.accept_capture({"context": CONTEXT, "text": current, "generating": True,
            "scope": "current_turn", "message_count": 1, "turn_id": "2" * 64}, CONTEXT)
        return original(text, code_blocks=code_blocks)
    backend.translator.translate_reply = newer_turn
    result = run(backend, payload)
    assert result["state"] == "failed" and result["result"] is None
    assert backend.service.reply["text"] == current and backend.service.reply["turn_id"] == "2" * 64
    backend.translator.translate_reply = original
    following = run(backend, request("translate_reply", source_hash=backend.service.reply["source_hash"],
        incremental=True, expected_context=CONTEXT, connection_revision=payload.connection_revision))
    assert following["result"]["turn_id"] == "2" * 64
    assert following["result"]["translated_source"] == current
    assert backend.remote.count("send") == 0


@pytest.mark.parametrize("turn_id,expected", [("a" * 64, "a" * 64), ("wrong", ""), (123, ""), (None, "")])
def test_capture_turn_identity_is_validated_without_breaking_legacy_reads(backend, turn_id, expected):
    backend.service.accept_capture({"context": CONTEXT, "text": "Synthetic current task reply", "turn_id": turn_id}, CONTEXT)
    assert backend.service.reply["turn_id"] == expected


def test_waiting_choice_card_is_read_only_translatable_and_clears_on_completion(backend):
    question = {"prompt": "Which narrator voice should be used?", "page": 1, "total_pages": 3,
                "options": [{"label": "Man, 45-55 (Recommended)", "description": "Warm storyteller.", "selected": True},
                            {"label": "Woman, 40-55", "description": "Female storyteller.", "selected": False}],
                "multiple": False, "actions": ["Skip", "Next"], "private_unlisted_field": "never expose"}
    backend.remote.reply = {"text": "", "mode": "Code", "generating": True,
                            "awaiting_input": True, "question": question}
    backend.service.capture()
    reply = backend.service.reply
    assert reply["awaiting_input"] and not reply["generating"] and reply["read_status"] == "waiting_input"
    assert "Which narrator" in reply["text"] and "Warm storyteller." in reply["text"] and "尚未提交" in reply["text"]
    assert reply["source_hash"] == hashlib.sha256(reply["text"].encode()).hexdigest()
    assert "private_unlisted_field" not in reply["question"]
    assert backend.service.target["awaiting_input"] and backend.service.target["ready"] is False
    assert backend.service.target["busy"] is False and "请到 Ubuntu" in backend.service.target["reason"]
    assert backend.remote.count("probe") == 0 and backend.remote.count("send") == 0
    public = backend.service.public_state("synthetic-csrf")
    assert public["reply"]["question"] == reply["question"] and public["reply"]["awaiting_input"]
    first = run(backend, request("translate_reply", source_hash=reply["source_hash"]))
    assert first["state"] == "translated" and len(backend.translator.reply_translations) == 1
    backend.service.capture()
    repeated = run(backend, request("translate_reply", source_hash=reply["source_hash"]))
    assert repeated["result"]["cached"] and len(backend.translator.reply_translations) == 1
    backend.remote.reply = {"text": "The choice is complete.", "mode": "Code", "generating": False}
    backend.service.capture()
    assert not backend.service.reply["awaiting_input"] and backend.service.reply["question"] is None
    assert not backend.service.target["awaiting_input"] and backend.service.target["ready"] is True
    assert backend.remote.count("probe") == 1 and backend.remote.count("send") == 0


def test_waiting_marker_without_readable_options_still_has_visible_source(backend):
    backend.remote.reply = {"text": "", "mode": "Code", "generating": True,
                            "awaiting_input": True, "question": {}}
    backend.service.capture()
    assert backend.service.reply["text"] == "Claude 正在等待你选择，请到 Ubuntu 查看并操作。"
    assert backend.service.reply["source_hash"] and backend.service.reply["question"] == {}
    assert backend.service.reply["awaiting_input"] and not backend.service.reply["generating"]
    before = dict(backend.service.reply)
    with pytest.raises(core.BridgeError):
        backend.service.accept_capture({"context": "f" * 64, "text": "Other task choice", "awaiting_input": True}, CONTEXT)
    assert backend.service.reply == before
    backend.service.capture_failed("请到 Ubuntu 查看当前会话。")
    assert not backend.service.reply.get("awaiting_input") and not backend.service.reply.get("question")
    assert not backend.service.target.get("awaiting_input")


def test_question_metadata_is_bounded_and_whitelisted():
    question = mobile.safe_question({"prompt": "p" * 9000, "page": True, "total_pages": 3,
        "options": [{"label": "l" * 3000, "description": "d" * 5000, "selected": 1,
                     "private_field": "never expose"}] * 30,
        "multiple": "true", "actions": ["Next", {"private": "field"}], "private_field": "never expose"})
    assert len(question["prompt"]) == 8000 and len(question["options"]) == 20
    assert len(question["options"][0]["label"]) == 2000 and len(question["options"][0]["description"]) == 4000
    assert question["options"][0]["selected"] is None and question["page"] is None
    assert question["total_pages"] is None and not question["multiple"] and question["actions"] == ["Next"]
    assert "private_field" not in json.dumps(question)
    assert mobile.safe_question({"prompt": "Question", "options": [None, {"label": 123}]}) is None


def test_live_reply_reuses_old_whole_reply_cache_as_incremental_prefix(backend):
    first = "A finished reply already translated in the previous version."
    payload = live_reply(backend, first)
    profile = backend.service.config()["profiles"][PROFILE]
    key = mobile.translation_key(first, profile, PROFILE, [])
    backend.service.reply_cache.put(key, first, {"text": "此前已完成的译文。"})
    cached = run(backend, payload)
    assert cached["result"]["translated_text"] == "此前已完成的译文。"
    assert backend.translator.reply_translations == []
    tail = "A newly appended sentence now needs translation."
    result = run(backend, live_reply(backend, first + "\n\n" + tail, generating=False))
    assert result["result"]["complete"]
    assert backend.translator.reply_translations == [(tail, [])]


def test_code_upload_rejected_before_storing_file(backend):
    headers = login(backend)
    backend.service.target = {"mode": "Code"}
    response = backend.client.post("/api/uploads", headers=headers,
                                   files={"file": ("synthetic.txt", b"Synthetic file", "text/plain")})
    assert response.status_code == 400
    assert not (backend.service.directory / "uploads").exists()


def test_code_attachment_job_rejected_before_translation_or_upload(backend):
    backend.remote.mode = "Code"
    payload = request("translate_send", expected_context=CONTEXT, attachment_ids=["d" * 32])
    result = run(backend, payload)
    assert result["state"] == "failed"
    assert "Code" in result["error"]
    assert backend.translator.translations == []
    assert backend.remote.count("send") == 0


def test_verified_code_attachment_uses_same_upload_and_draft_only_delivery(backend, monkeypatch):
    headers = login(backend)
    backend.remote.mode = "Code"
    backend.remote.native_attachments = True
    backend.service.target = {"mode": "Code", "capabilities": {"native_attachments": True}}
    uploaded = backend.client.post("/api/uploads", headers=headers,
                                   files={"file": ("synthetic.txt", b"Synthetic file", "text/plain")})
    assert uploaded.status_code == 200
    receipt = uploaded.json()
    bundles = []

    def upload(client, bundle):
        assert client is backend.remote
        bundles.append(bundle)
        return [{"name": "synthetic.txt", "kind": "file", "remote_path": "/synthetic/synthetic.txt"}]

    monkeypatch.setattr(mobile, "upload_bundle", upload)
    payload = request("send", english="Inspect attachment", expected_context=CONTEXT,
                      attachment_ids=[receipt["id"]], auto_enter=False)
    result = run(backend, payload)
    assert result["state"] == "pasted"
    sent = next(values for action, values in backend.remote.calls if action == "send")
    assert sent["attachment_batch"] == bundles[0]["batch_id"]
    assert sent["enter"] is False and sent["context"] == CONTEXT
    assert "Inspect attachment" in sent["text"]
    assert backend.translator.translations == []


def test_content_length_cap_rejects_request_before_parsing(backend):
    response = backend.client.post("/api/login", headers={"Origin": ORIGIN, "Content-Length": str(1024 * 1024 + 1)},
                                   content=b"{}")
    assert response.status_code == 413


def test_streamed_body_without_content_length_is_capped(backend):
    def chunks():
        yield b'{"code":"'
        for _ in range(17):
            yield b"x" * 65536
        yield b'"}'

    response = backend.client.post("/api/login", headers={"Origin": ORIGIN, "Content-Type": "application/json"},
                                   content=chunks())
    assert response.status_code == 413
    with backend.service.database() as db:
        assert db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0


def test_upload_stream_cap_runs_before_file_storage(backend, monkeypatch):
    headers = login(backend)
    monkeypatch.setattr(mobile, "MAX_UPLOAD", 16)
    headers["Content-Type"] = "multipart/form-data; boundary=syntheticboundary"

    def chunks():
        yield (b"--syntheticboundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"synthetic.txt\"\r\n"
               b"Content-Type: text/plain\r\n\r\n")
        yield b"x" * 70000
        yield b"\r\n--syntheticboundary--\r\n"

    response = backend.client.post("/api/uploads", headers=headers, content=chunks())
    assert response.status_code == 413
    assert not (backend.service.directory / "uploads").exists()


def test_unexpected_capture_error_keeps_service_available(backend, monkeypatch):
    def broken_capture(action, **kwargs):
        raise RuntimeError("Synthetic unexpected remote error")

    monkeypatch.setattr(backend.remote, "action", broken_capture)
    backend.service.capture()
    assert backend.service.target["ready"] is False
    assert not backend.service.remote_lock.locked()
    assert backend.client.get("/healthz").status_code == 200


def test_single_service_instance_lock_rejects_duplicate_and_releases(tmp_path):
    with mobile.service_instance(tmp_path):
        with pytest.raises(core.BridgeError, match="后台已在运行"):
            with mobile.service_instance(tmp_path):
                raise AssertionError("Duplicate service acquired the lock")
    with mobile.service_instance(tmp_path):
        assert (tmp_path / "mobile" / "worker.lock").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Exercises real Windows owner ACLs")
def test_pairing_note_keeps_owner_read_write_and_allows_new_pair_code(tmp_path):
    note = mobile.issue_pairing_code(tmp_path)
    assert note == tmp_path / "mobile" / "设备配对信息.txt" and note.is_file()
    original = note.read_bytes()
    with note.open("r+b") as handle:
        first = handle.read(1)
        if not first:
            raise AssertionError("The pairing note is empty")
        handle.seek(0)
        handle.write(first)
        handle.flush()
    child = note.parent / "synthetic-owner-write.txt"
    child.write_text("Synthetic owner write", encoding="utf-8")
    assert child.read_text(encoding="utf-8") == "Synthetic owner write"
    child.unlink()
    assert mobile.issue_pairing_code(tmp_path) == note
    if note.read_bytes() == original:
        raise AssertionError("Issuing another pairing code did not replace the local note")


def test_device_cli_only_prints_local_note_path_or_safe_device_metadata(backend, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mobile_service.py", "--data-dir", str(backend.directory), "--pair-device"])
    mobile.main()
    output = capsys.readouterr().out
    note = backend.directory / "mobile" / "设备配对信息.txt"
    code = next(line.split("：", 1)[1].strip() for line in note.read_text(encoding="utf-8").splitlines()
                if line.startswith("配对码："))
    if code in output:
        raise AssertionError("The pairing CLI printed the raw pairing code")
    assert output and note.is_file()
    response = backend.client.post("/api/login", headers={"Origin": ORIGIN}, json={"code": code, "label": "CLI test phone"})
    assert response.status_code == 200
    token = backend.client.cookies.get(COOKIE)
    monkeypatch.setattr(sys, "argv", ["mobile_service.py", "--data-dir", str(backend.directory), "--list-devices"])
    mobile.main()
    listed_output = capsys.readouterr().out
    listed = json.loads(listed_output)
    assert len(listed) == 1 and listed[0]["label"] == "CLI test phone"
    if token in listed_output or code in listed_output or "token_hash" in listed_output or '"csrf"' in listed_output:
        raise AssertionError("The device-list CLI exposed an authentication credential")
    monkeypatch.setattr(sys, "argv", ["mobile_service.py", "--data-dir", str(backend.directory), "--revoke-device", listed[0]["id"]])
    mobile.main()
    revoked_output = capsys.readouterr().out
    if token in revoked_output or code in revoked_output:
        raise AssertionError("The revocation CLI exposed an authentication credential")
    assert backend.client.get("/api/state").status_code == 401
