import hashlib
import json
import threading
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import pytest
from PIL import Image

from core import BridgeError, send_fingerprint
from media import build_bundle, compose_prompt, snapshot, upload_bundle, upload_file


def test_images_keep_original_bytes_and_generate_separate_preview(tmp_path):
    source = tmp_path / "原图.png"
    Image.new("RGB", (2000, 1000), "#2466ae").save(source)
    before = source.read_bytes()
    bundle = build_bundle([source], data_dir=tmp_path / "data")
    assert source.read_bytes() == before
    assert len(bundle["originals"]) == 1 and len(bundle["files"]) == 2
    assert bundle["originals"][0]["sha256"] == hashlib.sha256(before).hexdigest()
    preview = next(f for f in bundle["files"] if f["role"] == "preview")
    with Image.open(preview["path"]) as image:
        assert max(image.size) == 1568
    assert preview["parent_id"] == bundle["originals"][0]["id"]


def test_missing_file_and_directory_rejected(tmp_path):
    for path in [tmp_path, tmp_path / "missing"]:
        with pytest.raises(BridgeError):
            snapshot(path)


def test_previews_can_be_disabled_and_same_names_remain_distinct(tmp_path):
    sources = []
    for folder in ["a", "b"]:
        directory = tmp_path / folder
        directory.mkdir()
        source = directory / "same.png"
        source.write_bytes(folder.encode())
        sources.append(source)
    bundle = build_bundle(sources, previews=False, data_dir=tmp_path / "data")
    assert len(bundle["files"]) == 2
    assert len({f["id"] for f in bundle["files"]}) == 2
    assert len({f["sha256"] for f in bundle["files"]}) == 2


def test_cancel_stops_before_file_processing(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(BridgeError, match="已停止"):
        build_bundle([tmp_path / "missing"], cancel=cancel)


def test_attachment_identity_changes_duplicate_detection():
    target = {"session": "bridge-main"}
    a = [{"local_path": "same.png", "sha256": "a"}]
    b = [{"local_path": "same.png", "sha256": "b"}]
    assert send_fingerprint("hello", target, a) != send_fingerprint("hello", target, b)
    assert send_fingerprint("hello", target) != send_fingerprint("hello", target, a)


def test_prompt_names_are_quoted_and_videos_described_as_samples():
    receipts = [{"name": '视频 "test" $(whoami).mp4', "kind": "video", "size": 123,
                 "remote_path": "/workspace/attachment.mp4", "preview_path": "/workspace/preview.jpg",
                 "metadata_path": "/workspace/meta.json"}]
    text = compose_prompt("Analyze these files.", receipts)
    assert text.startswith("Analyze these files.")
    assert json.dumps(receipts[0]["name"], ensure_ascii=False) in text
    assert "sampled frames only" in text and "no audio" not in text
    assert "transcribe audio" in text and '"/workspace/preview.jpg"' in text
    assert compose_prompt("unchanged", []) == "unchanged"


def test_changed_file_is_not_uploaded(tmp_path):
    path = tmp_path / "test.txt"
    path.write_text("before")
    entry = snapshot(path)
    path.write_text("after - changed length")
    with patch("media.subprocess.Popen") as popen:
        with pytest.raises(BridgeError, match="已变化"):
            upload_file(SimpleNamespace(config={"session": "bridge-main"}), "f" * 32, entry)
        assert not popen.called
