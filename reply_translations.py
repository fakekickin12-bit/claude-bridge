"""Local reply-translation cache. No API credentials are written here."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core import atomic_json, effective_reasoning_effort


def translation_settings(profile):
    fields = {k: profile.get(k, "") for k in ["kind", "base_url", "model", "protocol", "reasoning_effort", "codex_path"]}
    # Legacy Codex profiles without a value use Low; explicit empty means CLI default.
    fields["reasoning_effort"] = effective_reasoning_effort(profile)
    return fields


def translation_revision(profile):
    """Opaque settings identity: no key, environment value, or local path is exposed."""
    return hashlib.sha256(json.dumps(["translation-settings-v1", translation_settings(profile)],
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def translation_key(source, profile, profile_id="", code_blocks=None):
    fields = translation_settings(profile)
    return hashlib.sha256(json.dumps(["reply-zh-v1", profile_id, fields, source, code_blocks or []],
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class ReplyCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.memory = {}

    def get(self, key, source):
        record = self.memory.get(key)
        if record is None:
            try:
                record = json.loads((self.directory / (key + ".json")).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        if not isinstance(record, dict) or record.get("source") != source or not isinstance(record.get("text"), str) or not record["text"].strip():
            return None
        self.memory[key] = record
        return record

    def put(self, key, source, result):
        record = {"source": source, "text": result["text"], "model": result.get("model", ""), "usage": result.get("usage", {})}
        self.memory[key] = record
        atomic_json(self.directory / (key + ".json"), record)
