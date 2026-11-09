"""Local reply-translation cache. No API credentials are written here."""
from __future__ import annotations

import hashlib
import json
import re
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


def _protected_ranges(source, code_blocks):
    """Find atomic literals and a possibly unfinished fenced block.

    This is only a boundary finder. Literal restoration remains the translator's
    responsibility, using the exact code_blocks carried by the chunk.
    """
    ranges = []
    opened = None
    for match in re.finditer(r"(?m)^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)", source):
        marker, suffix = match.group(1), match.group(2)
        if opened is None:
            opened = (match.start(), marker[0], len(marker))
        elif marker[0] == opened[1] and len(marker) >= opened[2] and not suffix.strip():
            ranges.append((opened[0], match.end()))
            opened = None
    unfinished = opened[0] if opened else None
    if opened:
        ranges.append((opened[0], len(source)))
    fenced = list(ranges)
    inline = None
    for match in re.finditer(r"`+", source):
        if any(left <= match.start() < right for left, right in fenced):
            continue
        if inline is None:
            inline = (match.start(), len(match.group()))
        elif len(match.group()) == inline[1]:
            ranges.append((inline[0], match.end()))
            inline = None
    if inline:
        ranges.append((inline[0], len(source)))
        unfinished = min(unfinished, inline[0]) if unfinished is not None else inline[0]
    for pattern in (r"https?://[^\s<>]+", r'"[^"\r\n]*"|“[^”\r\n]*”|‘[^’\r\n]*’'):
        ranges.extend((match.start(), match.end()) for match in re.finditer(pattern, source))
    for block in code_blocks:
        if not block:
            continue
        start = 0
        while (start := source.find(block, start)) >= 0:
            ranges.append((start, start + len(block)))
            start += len(block)
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged, unfinished


class IncrementalReply:
    """Translate only the newly completed part of the latest native reply.

    Callers serialize state access, and run model requests outside their UI or
    backend lock. A chunk is a detached dict; accept() rechecks its scope and
    exact original prefix, so a growing reply is safe while a request runs.
    """

    MIN_SENTENCE = 20
    WORD_BREAK_MIN = 500
    MAX_CHUNK = 650

    def __init__(self):
        self.source = ""
        self.generating = False
        self.scope = ""
        self.code_blocks = []
        self._segments = []

    @property
    def consumed(self):
        return self._segments[-1]["end"] if self._segments else 0

    @property
    def translated_source(self):
        return self.source[:self.consumed]

    @property
    def translated_text(self):
        return "".join(segment["text"] for segment in self._segments)

    @property
    def complete(self):
        return self.consumed == len(self.source)

    def sync(self, source, generating=False, scope="", code_blocks=None):
        source = source if isinstance(source, str) else ""
        if scope != self.scope:
            self._segments.clear()
        else:
            # Preserve only whole accepted chunks whose original bytes still
            # agree. Rewrites solely in the unprocessed tail retain all work.
            keep = 0
            for segment in self._segments:
                if source[:segment["end"]] != self.source[:segment["end"]]:
                    break
                keep += 1
            del self._segments[keep:]
        self.source, self.generating, self.scope = source, bool(generating), scope
        self.code_blocks = [block for block in code_blocks or [] if isinstance(block, str) and block]
        return self

    def render(self):
        return self.translated_text + self.source[self.consumed:]

    def seed_cached(self, result):
        """Restore an exact whole-source cache hit before translating a tail.

        The caller checks the cache key against the synced source and settings.
        Existing partial work is never overwritten by a later cache lookup.
        """
        if self.consumed or not self.source.strip() or not isinstance(result, dict):
            return False
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            return False
        leading = self.source[:len(self.source) - len(self.source.lstrip())]
        trailing = self.source[len(self.source.rstrip()):]
        self._segments.append({"end": len(self.source), "text": leading + text.strip() + trailing})
        return True

    def next_chunk(self):
        start = self.consumed
        if start == len(self.source):
            return None
        tail = self.source[start:]
        if not tail.strip():
            # Whitespace is already language-neutral and must not cause a
            # model request, or leave a finished reply permanently pending.
            self._segments.append({"end": len(self.source), "text": tail})
            return None
        protected, unfinished = _protected_ranges(self.source, self.code_blocks)
        ceiling = unfinished if self.generating and unfinished is not None else len(self.source)
        if ceiling <= start:
            return None

        def safe(position):
            return not any(left < position < right for left, right in protected)

        limit = min(start + self.MAX_CHUNK, ceiling)
        # A final, reasonably sized tail can be flushed without waiting for
        # punctuation. While generating, translate only complete boundaries.
        if not self.generating and ceiling <= limit:
            end = ceiling
        else:
            boundaries = {match.end() for match in re.finditer(r"\n[ \t\r]*\n[ \t\r\n]*", self.source)}
            boundaries.update(match.end() for match in re.finditer(
                r'''(?:[.!?](?:["'”’\)\]]*)|[。！？](?:["'”’\)\]]*))(?:[ \t\r\n]+|(?=$))''', self.source))
            if unfinished is not None:
                boundaries.add(unfinished)
            options = [position for position in boundaries if start < position <= limit and safe(position)
                       and len(self.source[start:position].strip()) >= self.MIN_SENTENCE]
            end = max(options) if options else None
            if end is None and ceiling - start >= self.WORD_BREAK_MIN:
                words = [match.end() for match in re.finditer(r"\s+", self.source)
                         if start + self.WORD_BREAK_MIN <= match.end() <= limit and safe(match.end())]
                end = max(words) if words else None
            if end is None and ceiling - start > self.MAX_CHUNK:
                # A large atomic code block, URL, or unbroken token may exceed
                # the normal limit. Never split such a literal to meet a cap.
                candidates = [right for left, right in protected if start < right <= ceiling
                              and not self.source[start:left].strip()]
                candidates.extend(match.end() for match in re.finditer(r"\s+", self.source)
                                  if limit < match.end() <= ceiling and safe(match.end()))
                end = min(candidates) if candidates else (ceiling if not self.generating else None)
        if end is None or end <= start:
            return None
        raw = self.source[start:end]
        value = raw.strip()
        if not value:
            return None
        blocks = []
        for block in self.code_blocks:
            # Trimming only a chunk's outer whitespace can trim a native code
            # block's final newline too. Protect the largest exact remaining
            # literal, and restore that whitespace outside the translation.
            for candidate in (block, block.rstrip(), block.lstrip(), block.strip()):
                if candidate and candidate in value:
                    blocks.append(candidate)
                    break
        return {"source": value, "raw_source": raw, "prefix": self.source[:start],
                "offset": start, "end": end, "scope": self.scope,
                "code_blocks": blocks}

    def accept(self, chunk, result):
        if not isinstance(chunk, dict) or not isinstance(result, dict):
            return False
        raw, offset, end = chunk.get("raw_source"), chunk.get("offset"), chunk.get("end")
        text = result.get("text")
        if (chunk.get("scope") != self.scope or not isinstance(offset, int) or offset != self.consumed or not isinstance(raw, str)
                or not isinstance(end, int) or end != offset + len(raw)
                or chunk.get("prefix") != self.source[:offset] or self.source[offset:end] != raw
                or chunk.get("source") != raw.strip() or not isinstance(text, str) or not text.strip()):
            return False
        leading = raw[:len(raw) - len(raw.lstrip())]
        trailing = raw[len(raw.rstrip()):]
        self._segments.append({"end": end, "text": leading + text.strip() + trailing})
        return True
