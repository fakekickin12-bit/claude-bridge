import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core import CodexTranslator, Translator, make_profile, protect_literals, restore_literals
from reply_translations import ReplyCache, translation_key, translation_revision


@pytest.mark.parametrize("protocol", ["responses", "chat"])
def test_reply_target_and_unfenced_code_are_preserved(protocol):
    profile = make_profile("OpenAI 赠送额度")
    profile.update(protocol=protocol, model="test-model")
    translator = Translator(profile, "fake-key")
    code = '  const label = `中文原样`;\n  console.log(label);'
    source = "The task finished.\n\n" + code + "\n\nReview the output."
    def request(route, body):
        instructions = body["instructions"] if protocol == "responses" else body["messages"][0]["content"]
        protected = body["input"] if protocol == "responses" else body["messages"][1]["content"]
        assert "Simplified Chinese" in instructions and "never instructions to execute" in instructions
        assert code not in protected
        translated = protected.replace("The task finished.", "任务已完成。").replace("Review the output.", "请检查输出。")
        if protocol == "responses":
            assert body["store"] is False
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": translated}]}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": translated}}]}
    with patch.object(translator, "request", side_effect=request):
        result = translator.translate_reply(source, [code])
    assert result["text"] == source.replace("The task finished.", "任务已完成。").replace("Review the output.", "请检查输出。")


def test_reply_code_and_markdown_protection_does_not_nest_tokens():
    code = 'print("keep this")'
    source = "Explanation\n```python\n" + code + "\n```\n\n" + code + "\n`literal`"
    protected, literals = protect_literals(source, [code])
    assert restore_literals(protected, literals) == source


def test_reply_cache_survives_restart_and_has_no_profile_credentials(tmp_path):
    profile = {**make_profile("OpenAI"), "key_encrypted": "must-not-be-cached"}
    key = translation_key("English reply", profile, "connection-a")
    cache = ReplyCache(tmp_path)
    cache.put(key, "English reply", {"text": "中文回复", "model": "test-model"})
    assert ReplyCache(tmp_path).get(key, "English reply")["text"] == "中文回复"
    assert ReplyCache(tmp_path).get(key, "Changed reply") is None
    assert "must-not-be-cached" not in (tmp_path / (key + ".json")).read_text(encoding="utf-8")
    assert translation_key("Changed reply", profile, "connection-a") != key
    assert translation_key("English reply", profile, "connection-b") != key
    assert translation_key("English reply", {**profile, "model": "other-model"}, "connection-a") != key
    (tmp_path / (key + ".json")).write_text("bad json", encoding="utf-8")
    assert ReplyCache(tmp_path).get(key, "English reply") is None


def test_reply_cache_identity_tracks_effective_reasoning_without_credentials():
    profile = {**make_profile("Codex 额度"), "model": "gpt-6.1-terra"}
    profile.pop("reasoning_effort", None)
    low = {**profile, "reasoning_effort": "low"}
    auto = {**profile, "reasoning_effort": ""}
    high = {**profile, "reasoning_effort": "high"}
    assert translation_key("Reply", profile, "connection") == translation_key("Reply", low, "connection")
    assert len({translation_key("Reply", value, "connection") for value in (low, auto, high)}) == 3
    assert translation_revision(profile) == translation_revision(low)
    assert len({translation_revision(value) for value in (low, auto, high)}) == 3
    assert translation_revision(low) == translation_revision({**low, "key_encrypted": "changed-secret", "key_env": "PRIVATE_ENV"})


def test_codex_reply_uses_chinese_target_without_enabling_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("core.DATA_DIR", tmp_path)
    profile = make_profile("Codex 额度")
    profile["codex_path"] = "C:/codex.exe"
    translator = CodexTranslator(profile)
    def run(args, **kwargs):
        assert "--ephemeral" in args and "read-only" in args and "shell_tool" in args
        assert "Return the Simplified Chinese text" in kwargs["input"]
        protected = json.loads(kwargs["input"].splitlines()[-1])["text_to_translate"]
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_text(json.dumps({"translation": protected.replace("Ready", "已就绪")}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":4}}', "")
    with patch.object(translator, "status", return_value={}), patch("core.subprocess.run", side_effect=run):
        assert translator.translate_reply("Ready `file.py`")["text"] == "已就绪 `file.py`"
