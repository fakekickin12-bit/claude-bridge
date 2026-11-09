import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from core import (BridgeError, CodexTranslator, SSH, Translator, api_base, defaults, load_config, make_profile, protect_key,
                  protect_literals, restore_literals, unprotect_key, validate_text)


@pytest.fixture
def fake_api():
    state = {"mode": "ok", "calls": [], "redirect_hits": 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        def do_GET(self):
            if self.path == "/models":
                self.reply(200, {"data": [{"id": "test-model"}]})
            else:
                state["redirect_hits"] += 1
                self.reply(200, {})
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append({"path": self.path, "body": data, "auth": self.headers.get("Authorization")})
            text = data["messages"][1]["content"].replace("修复", "Fix")
            if state["mode"] == "redirect":
                self.send_response(307)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            elif state["mode"] == "unauthorized":
                self.reply(401, {"error": "DO NOT EXPOSE fake-secret"})
            else:
                self.reply(200, {"choices": [{"message": {"content": text if state["mode"] != "lost" else "Fix code"},
                                             "finish_reason": "length" if state["mode"] == "length" else "stop"}],
                                 "usage": {"total_tokens": 12}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["client"] = Translator({"base_url": f"http://127.0.0.1:{server.server_port}", "model": "test-model"}, "fake-secret")
    yield state
    server.shutdown()
    server.server_close()


def test_translation_preserves_code_and_only_uses_basic_parameters(fake_api):
    source = '修复 `D:\\素材\\文件.py`\n```python\nprint("中文")\n```'
    result = fake_api["client"].translate(source)
    assert result["text"] == source.replace("修复", "Fix")
    call = fake_api["calls"][0]
    assert call["path"] == "/chat/completions"
    assert set(call["body"]) == {"model", "messages", "stream"}
    assert "print" not in call["body"]["messages"][1]["content"]
    assert call["auth"] == "Bearer fake-secret"


def test_model_listing(fake_api):
    assert fake_api["client"].models() == ["test-model"]


@pytest.mark.parametrize("mode", ["lost", "length"])
def test_incomplete_translation_fails_closed(fake_api, mode):
    fake_api["mode"] = mode
    with pytest.raises(BridgeError):
        fake_api["client"].translate("修复 `code.py`")


def test_redirect_not_followed(fake_api):
    fake_api["mode"] = "redirect"
    with pytest.raises(BridgeError, match="307"):
        fake_api["client"].translate("修复")
    assert fake_api["redirect_hits"] == 0
    assert len(fake_api["calls"]) == 1


def test_error_does_not_expose_key_or_server_body(fake_api):
    fake_api["mode"] = "unauthorized"
    with pytest.raises(BridgeError) as exc:
        fake_api["client"].translate("修复")
    assert "fake-secret" not in str(exc.value)
    assert "DO NOT EXPOSE" not in str(exc.value)


@pytest.mark.parametrize("url", ["http://api.example.com", "https://synthetic-user:synthetic-secret@example.com", "https://example.com?key=secret", "file:///x"])
def test_invalid_api_url(url):
    with pytest.raises(BridgeError):
        api_base(url)


def test_complete_api_url_normalized():
    assert api_base("https://example.com/v1/chat/completions/") == "https://example.com/v1"


@pytest.mark.parametrize("host", ["-oProxyCommand=evil", "ubuntu; touch /tmp/x", "$(evil)", "user@host\nwhoami"])
def test_ssh_target_injection_rejected(host):
    with pytest.raises(BridgeError):
        SSH({"host": host})


def test_prompt_is_json_stdin_not_remote_shell():
    prompt = 'English\n"; $(touch /tmp/never) `whoami` 中文'
    with patch("core.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, '{"ok":true,"state":"submitted"}', "")
        SSH(defaults()["ssh"]).action("send", text=prompt, request_id="f" * 32)
        args, kwargs = run.call_args
        assert prompt not in args[0][-1]
        assert json.loads(kwargs["input"])["text"] == prompt
        assert "StrictHostKeyChecking=yes" in args[0]


@pytest.mark.parametrize("text", ["", "hello\x1b[201~", "abc\x00", "x\x7f"])
def test_control_characters_rejected(text):
    with pytest.raises(BridgeError):
        validate_text(text)


def test_dpapi_roundtrip():
    value = "self-test-only-中文-secret"
    encrypted = protect_key(value)
    assert value not in encrypted
    assert unprotect_key(encrypted) == value


def test_repeated_code_tokens_rejected():
    protected, literals = protect_literals("保留 `hello`")
    with pytest.raises(BridgeError):
        restore_literals(protected + protected, literals)


def test_responses_translation_preserves_literals():
    p = make_profile("OpenAI 赠送额度")
    p["model"] = "test-model"
    client = Translator(p, "test-key")
    def request(route, body):
        assert route == "/responses" and body["store"] is False
        assert "messages" not in body
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": body["input"].replace("修复", "Fix") }]}]}
    with patch.object(client, "request", side_effect=request):
        assert client.translate("修复 `中文.py`")["text"] == "Fix `中文.py`"


def test_responses_incomplete_never_returns_translation():
    p = make_profile("OpenAI 赠送额度")
    p["model"] = "test-model"
    client = Translator(p, "test-key")
    with patch.object(client, "request", return_value={"status": "incomplete", "output": []}):
        with pytest.raises(BridgeError):
            client.translate("修复")


@pytest.mark.parametrize("model,has_reasoning", [("gpt-6-astra", True), ("gpt-4.1-mini", False)])
def test_latest_default_uses_supported_reasoning_without_breaking_older_models(model, has_reasoning):
    p = make_profile("OpenAI 赠送额度")
    p["model"] = model
    client = Translator(p, "test-key")
    def request(route, body):
        assert ("reasoning" in body) is has_reasoning
        if has_reasoning:
            assert body["reasoning"] == {"effort": "low"}
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Check the startup error."}]}]}
    with patch.object(client, "request", side_effect=request):
        assert client.translate("检查启动错误")["text"]


def test_config_migration_preserves_old_keys_and_session(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": "OpenAI", "profiles": {"OpenAI": {"key_encrypted": "opaque-old-key", "model": "original"}},
                                "ssh": {"host": "kept-host", "session": "bridge-kept"}}))
    config = load_config(path)
    assert config["provider"] == "OpenAI"
    assert config["profiles"]["OpenAI"]["key_encrypted"] == "opaque-old-key"
    assert config["profiles"]["OpenAI"]["kind"] == "api"
    assert "Codex 额度" in config["profiles"]
    assert config["ssh"]["session"] == "bridge-kept"


def test_multiple_same_service_profiles_and_deleted_defaults_stay_deleted(tmp_path):
    config = defaults()
    config["profiles"] = {"a": make_profile("OpenAI", "工作"), "b": make_profile("OpenAI", "备用")}
    config["provider"] = "b"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    result = load_config(path)
    assert set(result["profiles"]) == {"a", "b"}
    assert result["profiles"]["a"]["label"] == "工作"
    assert result["profiles"]["b"]["label"] == "备用"
    assert result["provider"] == "b"


def test_codex_uses_cli_chatgpt_auth_and_stdin_without_tools(tmp_path, monkeypatch):
    import core
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://must-not-be-used.invalid")
    p = make_profile("Codex 额度")
    p["codex_path"] = "C:/codex.exe"
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "OPENAI_BASE_URL" not in kwargs["env"]
        if args[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(args, 0, "", "Logged in using ChatGPT")
        assert "--ignore-user-config" in args and "--ephemeral" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert "shell_tool" in args and "multi_agent" in args and "hooks" in args
        assert args[-1] == "-"
        from pathlib import Path
        protected = json.loads(kwargs["input"].split('\n{"text_to_translate":', 1)[1].join(['{"text_to_translate":', '']))["text_to_translate"]
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps({"translation": protected.replace("修复", "Fix")}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}', "")
    with patch("core.subprocess.run", side_effect=run):
        result = CodexTranslator(p).translate("修复 `code.py`")
    assert result["text"] == "Fix `code.py`"
    assert result["usage"]["total_tokens"] == 15
    assert len(calls) == 2
    assert list((tmp_path / "codex-runs").iterdir()) == []


def test_codex_api_key_login_is_rejected():
    p = make_profile("Codex 额度")
    p["codex_path"] = "C:/codex.exe"
    with patch("core.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "Logged in using an API key")) as run:
        with pytest.raises(BridgeError, match="ChatGPT"):
            CodexTranslator(p).translate("hello")
        assert run.call_count == 1


def test_codex_quota_failure_does_not_return_a_translation(tmp_path, monkeypatch):
    import core
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    p = make_profile("Codex 额度")
    p["codex_path"] = "C:/codex.exe"
    responses = [subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT"),
                 subprocess.CompletedProcess([], 1, '{"type":"turn.failed"}', "usage_limit_reached")]
    with patch("core.subprocess.run", side_effect=responses) as run:
        with pytest.raises(BridgeError, match="额度不足"):
            CodexTranslator(p).translate("hello")
        assert run.call_count == 2


def test_ssh_preserves_definite_no_submission_flag():
    from core import SSH
    with pytest.raises(BridgeError) as error:
        SSH.decode_result(json.dumps({"ok": False, "error": "Draft readback failed", "not_submitted": True}), "", 0)
    assert error.value.not_submitted is True
    with pytest.raises(BridgeError) as error:
        SSH.decode_result(json.dumps({"ok": False, "error": "Unknown result"}), "", 0)
    assert error.value.not_submitted is False
