"""Ship the desktop transport as owned helper resources, never arbitrary paths."""
import io
import json
from pathlib import Path

import pytest

import core


def test_ssh_install_ships_stream_helper_with_transport(tmp_path, monkeypatch):
    names = ("remote.py", "desktop_remote.py", "desktop_stream.py")
    for name in names:
        (tmp_path / name).write_text("# synthetic " + name, encoding="utf-8")
    monkeypatch.setattr(core, "RESOURCE_DIR", tmp_path)
    client = core.SSH({"host": "synthetic-host"})
    calls = []
    monkeypatch.setattr(client, "run", lambda command, payload: calls.append(payload) or {"ok": True})
    monkeypatch.setattr(client, "action", lambda action, **kwargs: {"action": action, **kwargs})
    assert client.install()["action"] == "setup"
    assert set(calls[0]["sources"]) == set(names)
    assert calls[0]["sources"]["desktop_stream.py"] == "# synthetic desktop_stream.py"


def test_bootstrap_writes_only_named_helpers(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    files = {name: "# synthetic " + name for name in ("remote.py", "desktop_remote.py", "desktop_stream.py")}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"sources": files})))
    exec(core.BOOTSTRAP, {})
    assert json.loads(capsys.readouterr().out)["ok"]
    directory = tmp_path / ".local/share/claude-bridge"
    assert {p.name for p in directory.iterdir()} == set(files)
    assert all((directory / name).read_text(encoding="utf-8") == value for name, value in files.items())
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"sources": {"../outside.py": "untrusted"}})))
    with pytest.raises(ValueError, match="invalid helper"):
        exec(core.BOOTSTRAP, {})
    assert not (directory.parent / "outside.py").exists()
