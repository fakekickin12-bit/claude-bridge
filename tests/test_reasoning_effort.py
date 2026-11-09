"""Reasoning capabilities and wire propagation, without external model calls."""
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

import core


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max", ""])
def test_terra_effort_reaches_correct_api_parameter(protocol, effort):
    profile = {"base_url": "https://api.openai.com/v1", "model": "gpt-6.1-terra", "protocol": protocol,
               "reasoning_effort": effort}
    client = core.Translator(profile, "synthetic-key")
    def request(route, body):
        if protocol == "responses":
            assert route == "/responses"
            assert body.get("reasoning") == ({"effort": effort} if effort else None)
            assert "reasoning_effort" not in body
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Check"}]}]}
        assert route == "/chat/completions"
        assert body.get("reasoning_effort") == (effort or None)
        assert "reasoning" not in body
        return {"choices": [{"message": {"content": "Check"}, "finish_reason": "stop"}]}
    with patch.object(client, "request", side_effect=request):
        assert client.translate("检查")["text"] == "Check"


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize("effort", ["none", "low", "high", "max", ""])
def test_deepseek_supports_its_own_documented_levels(model, effort):
    profile = {"model": model, "base_url": "https://api.deepseek.com", "reasoning_effort": effort}
    assert core.reasoning_options(profile) == ("none", "low", "high", "max")
    client = core.Translator(profile, "synthetic-key")
    def request(route, body):
        assert body.get("reasoning_effort") == (effort or None)
        assert "thinking" not in body
        return {"choices": [{"message": {"content": "Check"}, "finish_reason": "stop"}]}
    with patch.object(client, "request", side_effect=request):
        assert client.translate("检查")["text"]


def test_deepseek_default_and_unknown_compatible_profiles_remain_unchanged():
    assert core.effective_reasoning_effort(core.make_profile("DeepSeek")) == ""
    inherited = core.make_profile("OpenAI")
    inherited["model"] = "unknown-compatible-model"
    assert core.reasoning_options(inherited) == ()
    assert core.effective_reasoning_effort(inherited) == ""
    assert core.reasoning_options({"model": "deepseek-flash", "base_url": "https://other.example.test"}) == ()


@pytest.mark.parametrize("model,effort", [("gpt-6.1-terra", "ultra"), ("gpt-6-astra", "none"),
                                         ("deepseek-flash", "medium"), ("deepseek-v4-pro", "xhigh"),
                                         ("gpt-6.1-terra", 'high";bad')])
def test_invalid_or_unsupported_levels_fail_before_network(model, effort):
    profile = {"model": model, "base_url": "https://api.deepseek.com" if model.startswith("deepseek") else "https://api.openai.com/v1",
               "reasoning_effort": effort}
    with pytest.raises(core.BridgeError, match="推理强度|Ultra"):
        core.Translator(profile, "synthetic-key")


def test_codex_cache_advertised_levels_are_authoritative_and_ultra_is_filtered(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{"slug": "synthetic-cli-model",
        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "ultra"},
                                       {"effort": ["invalid"]}, "max"]}]}))
    profile = {"kind": "codex", "model": "synthetic-cli-model", "codex_path": "C:/codex.exe"}
    assert core.reasoning_options(profile) == ("low", "high", "max")
    assert core.effective_reasoning_effort(profile) == "low"
    assert core.effective_reasoning_effort({**profile, "reasoning_effort": ""}) == ""
    assert core.effective_reasoning_effort({**profile, "reasoning_effort": "max"}) == "max"
    for unsupported in ["medium", "ultra"]:
        with pytest.raises(core.BridgeError):
            core.CodexTranslator({**profile, "reasoning_effort": unsupported})


@pytest.mark.parametrize("effort", [None, "", "high", "max"])
def test_codex_command_uses_selected_effort_and_preserves_translation_isolation(tmp_path, monkeypatch, effort):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-cache"))
    profile = {"kind": "codex", "model": "gpt-6.1-terra", "codex_path": "C:/codex.exe"}
    if effort is not None:
        profile["reasoning_effort"] = effort
    assert core.reasoning_options(profile) == ("low", "medium", "high", "xhigh", "max")
    seen = []
    def run(args, **kwargs):
        seen.append(args)
        if args[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(args, 0, "Logged in using ChatGPT", "")
        expected = "low" if effort is None else effort
        overrides = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "-c"]
        matched = [value for value in overrides if value.startswith("model_reasoning_effort=")]
        assert matched == (["model_reasoning_effort=" + json.dumps(expected)] if expected else [])
        assert '--ignore-user-config' in args and '--ignore-rules' in args and '--ephemeral' in args
        assert args[args.index('--sandbox') + 1] == 'read-only'
        for feature in ['multi_agent', 'multi_agent_v2', 'shell_tool', 'apps', 'plugins', 'hooks', 'computer_use', 'browser_use']:
            index = args.index(feature)
            assert args[index - 1] == '--disable'
        assert 'mcp_servers={}' in overrides and 'web_search="disabled"' in overrides
        Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps({'translation': 'Check'}), encoding='utf-8')
        return subprocess.CompletedProcess(args, 0, '{"type":"turn.completed"}', '')
    with patch('core.subprocess.run', side_effect=run):
        assert core.CodexTranslator(profile).translate('检查')['text'] == 'Check'
    assert len(seen) == 2
