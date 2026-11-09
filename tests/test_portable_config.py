"""Public-distribution configuration: no network, credentials, or personal defaults."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import core
import device_auth


@pytest.fixture
def clean_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_DATA", str(tmp_path))
    monkeypatch.delenv("CLAUDE_BRIDGE_MOBILE_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_BRIDGE_ORIGIN", raising=False)
    return tmp_path


def test_public_desktop_defaults_require_operator_configuration():
    config = core.defaults()
    assert config["provider"] == "OpenAI"
    assert config["auto_enter"] is False
    assert config["auto_translate_reply"] is False
    assert config["ssh"]["host"] == "ubuntu"
    assert config["ssh"]["cwd"].startswith("~/")
    assert config["ssh"]["desktop_bin"] == "claude-desktop"
    assert all(not item.get("key_encrypted") for item in config["profiles"].values())


def test_missing_mobile_origin_refuses_pairing_before_creating_data(clean_settings):
    with pytest.raises(core.BridgeError, match="HTTPS"):
        device_auth.issue_pairing_code(clean_settings)
    assert not (clean_settings / "mobile").exists()


@pytest.mark.parametrize("origin", ["", "http://bridge.example.test", "https://synthetic-user:synthetic-password@bridge.example.test",
                                   "https://bridge.example.test/path", "https://bridge.example.com",
                                   "https://bridge.example.test?token=not-a-token", "https://bridge.exam\nple.test"])
def test_pairing_rejects_unsafe_or_placeholder_origins(clean_settings, monkeypatch, origin):
    monkeypatch.setenv("CLAUDE_BRIDGE_ORIGIN", origin)
    with pytest.raises(core.BridgeError):
        device_auth.pairing_url(clean_settings)


def test_origin_uses_private_settings_and_explicit_override(clean_settings, monkeypatch):
    settings = clean_settings / "mobile/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"public_origin": "https://bridge.example.test/"}), encoding="utf-8")
    assert device_auth.pairing_url(clean_settings) == "https://bridge.example.test"
    alternate = clean_settings / "operator-settings.json"
    alternate.write_text(json.dumps({"public_origin": "https://other.example.test"}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_BRIDGE_MOBILE_CONFIG", str(alternate))
    assert device_auth.pairing_url(clean_settings) == "https://other.example.test"
    monkeypatch.setenv("CLAUDE_BRIDGE_ORIGIN", "https://override.example.test")
    assert device_auth.pairing_url(clean_settings) == "https://override.example.test"


def test_invalid_private_mobile_settings_has_a_clear_error(clean_settings):
    settings = clean_settings / "mobile/settings.json"
    settings.parent.mkdir()
    settings.write_text("[]", encoding="utf-8")
    with pytest.raises(core.BridgeError, match="JSON"):
        core.mobile_settings(clean_settings)


def transport():
    path = Path(__file__).resolve().parents[1] / "deploy_mobile/transport.py"
    spec = importlib.util.spec_from_file_location("portable_transport_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_vps_metadata_or_route_is_inherited(clean_settings):
    module = transport()
    with pytest.raises(RuntimeError, match="vps_host"):
        module.vps_paths()
    settings = clean_settings / "mobile/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"vps_host": "vps.example.test", "vps_port": 2222}), encoding="utf-8")
    host, admin_key, known = module.vps_paths()
    assert host == "vps.example.test" and module.vps_port() == 2222
    assert admin_key == (Path.home() / ".ssh/id_ed25519").resolve()
    assert known == (Path.home() / ".ssh/known_hosts").resolve()
    connection = Mock()
    with patch.object(module.socket, "socket", return_value=connection), \
         patch.object(module, "physical_route", side_effect=AssertionError("Do not override operator routing")):
        assert module.direct_socket(host) is connection
    connection.connect.assert_called_once_with((host, 2222))
    connection.bind.assert_not_called()


@pytest.mark.parametrize("port", [0, 65536, "22", True])
def test_vps_port_must_be_explicitly_valid(clean_settings, port):
    module = transport()
    settings = clean_settings / "mobile/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"vps_port": port}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="vps_port"):
        module.vps_port()
