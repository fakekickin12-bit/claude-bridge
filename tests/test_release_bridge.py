"""Static and desktop release checks using disposable files and mocked builds."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import release_bridge as release

RUNNING_PROCESS_IDS = release.running_process_ids


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "source"
    source = root / "mobile_web"
    source.mkdir(parents=True)
    payloads = {"app.js": b"const CLIENT_VERSION = '__BRIDGE_BUILD__';\n",
                "app.css": b"body { color: blue; }\n", "icon.svg": b"<svg />\n",
                "desktop.js": b"const DESKTOP_VERSION = '__BRIDGE_BUILD__';\n",
                "desktop.css": b".desktop { display: block; }\n",
                "desktop.html": b"<script src='/desktop.js?v=__BRIDGE_BUILD__'></script>\n",
                "index.html": b"<script src='/app.js'></script>\n",
                "manifest.webmanifest": b'{"name":"Synthetic Bridge","display":"standalone"}'}
    for name, content in payloads.items():
        (source / name).write_bytes(content)
    (root / "ClaudeBridge.spec").write_text("# Synthetic specification", encoding="utf-8")
    (root / "dist").mkdir()
    (root / "dist" / "ClaudeBridge.exe").write_bytes(b"new-desktop")
    install = tmp_path / "installed"
    install.mkdir()
    web = install / "mobile-service" / "mobile_web"
    web.mkdir(parents=True)
    old = {name: b"old-" + name.encode() for name in release.STATIC_FILES}
    old["manifest.webmanifest"] = b'{"bridge_version":"old-version"}'
    for name, content in old.items():
        (web / name).write_bytes(content)
    (install / "ClaudeBridge.exe").write_bytes(b"old-desktop")
    data = install / "data"
    data.mkdir()
    (data / "config.json").write_bytes(b"synthetic-config-must-stay")
    (install / "mobile-service" / "backend.py").write_bytes(b"synthetic-backend-must-stay")
    monkeypatch.setattr(release, "running_process_ids", lambda executable: [])
    return SimpleNamespace(root=root, source=source, payloads=payloads, install=install, web=web, old=old)


def publish(project, **options):
    return release.release(project.root, install_dir=project.install, **options)


def test_version_injection_keeps_source_untouched(project):
    version, files = release.static_release(project.source)
    assert len(version) == 64 and all(value in "0123456789abcdef" for value in version)
    assert release.BUILD_TOKEN not in files["app.js"]
    assert version.encode() in files["app.js"]
    assert json.loads(files["manifest.webmanifest"])["bridge_version"] == version
    assert {name: (project.source / name).read_bytes() for name in release.STATIC_FILES} == project.payloads


def test_manifest_prior_version_and_format_do_not_change_release_hash(project):
    version, files = release.static_release(project.source)
    manifest = {"display": "standalone", "bridge_version": "synthetic-old-build", "name": "Synthetic Bridge"}
    (project.source / "manifest.webmanifest").write_text(json.dumps(manifest, indent=4), encoding="utf-8")
    assert release.static_release(project.source) == (version, files)


def test_version_tracks_only_whitelisted_mobile_sources(project):
    version, _ = release.static_release(project.source)
    (project.source / "synthetic-private.txt").write_text("Ignored file", encoding="utf-8")
    (project.root / "dist" / "ClaudeBridge.exe").write_bytes(b"different-desktop")
    assert release.static_release(project.source)[0] == version
    (project.source / "app.css").write_bytes(b"body { color: red; }\n")
    assert release.static_release(project.source)[0] != version


@pytest.mark.parametrize("script", [b"const version = 'missing';", b"__BRIDGE_BUILD__ __BRIDGE_BUILD__"])
def test_missing_or_repeated_build_token_is_rejected(project, script):
    (project.source / "app.js").write_bytes(script)
    with pytest.raises(release.ReleaseError, match="一个 __BRIDGE_BUILD__"):
        release.static_release(project.source)


def test_web_only_publish_is_atomic_in_manifest_last_order_and_preserves_data(project, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Web-only release touched the desktop build or process check")

    monkeypatch.setattr(release, "running_process_ids", unexpected)
    monkeypatch.setattr(release.subprocess, "run", unexpected)
    replaced = []
    real_replace = release.atomic_replace

    def record(source, target):
        replaced.append(Path(target).name)
        real_replace(source, target)

    monkeypatch.setattr(release, "atomic_replace", record)
    result = publish(project, web_only=True)
    assert result["web_updated"] and not result["desktop_updated"]
    assert replaced == ["app.js", "app.css", "icon.svg", "desktop.js", "desktop.css", "desktop.html", "index.html", "manifest.webmanifest"]
    assert (project.install / "ClaudeBridge.exe").read_bytes() == b"old-desktop"
    assert (project.install / "data" / "config.json").read_bytes() == b"synthetic-config-must-stay"
    assert (project.install / "mobile-service" / "backend.py").read_bytes() == b"synthetic-backend-must-stay"
    backup = Path(result["backup"])
    assert {path.name: path.read_bytes() for path in (backup / "mobile_web").iterdir()} == project.old
    assert not (backup / "data").exists() and not (backup / "ClaudeBridge.exe").exists()


def test_same_version_is_noop_without_replacement_or_backup(project, monkeypatch):
    first = publish(project, web_only=True)
    timestamps = {name: (project.web / name).stat().st_mtime_ns for name in release.STATIC_FILES}
    backups = list((project.install / "versions").iterdir())

    def unexpected(*args):
        raise AssertionError("Identical mobile release rewrote a file")

    monkeypatch.setattr(release, "atomic_replace", unexpected)
    second = publish(project, web_only=True)
    assert second["version"] == first["version"] and not second["web_updated"]
    assert second["backup"] is None
    assert list((project.install / "versions").iterdir()) == backups
    assert {name: (project.web / name).stat().st_mtime_ns for name in release.STATIC_FILES} == timestamps


@pytest.mark.parametrize("failed_name", ["index.html", "manifest.webmanifest"])
def test_failed_assets_restore_entire_previous_release_and_rerun_finishes(project, monkeypatch, failed_name):
    real_replace = release.atomic_replace
    attempted = []

    def fail_index(source, target):
        attempted.append(Path(target).name)
        if Path(target).name == failed_name:
            raise OSError("Synthetic index publication error")
        real_replace(source, target)

    monkeypatch.setattr(release, "atomic_replace", fail_index)
    with pytest.raises(release.ReleaseError, match="版本清单未更新"):
        publish(project, web_only=True)
    assert {name: (project.web / name).read_bytes() for name in release.STATIC_FILES} == project.old
    assert attempted.count(failed_name) == 1
    if failed_name == "index.html":
        assert "manifest.webmanifest" not in attempted
    assert not list(project.web.glob(".bridge-release-*"))
    monkeypatch.setattr(release, "atomic_replace", real_replace)
    result = publish(project, web_only=True)
    assert json.loads((project.web / "manifest.webmanifest").read_bytes())["bridge_version"] == result["version"]


def test_failed_publish_removes_files_missing_before_release(project, monkeypatch):
    (project.web / "app.js").unlink()
    (project.web / "icon.svg").unlink()
    previous = {name: (project.web / name).read_bytes() for name in release.STATIC_FILES if (project.web / name).exists()}
    real_replace = release.atomic_replace

    def fail_index(source, target):
        if Path(target).name == "index.html":
            raise PermissionError("Synthetic locked index")
        real_replace(source, target)

    monkeypatch.setattr(release, "atomic_replace", fail_index)
    with pytest.raises(release.ReleaseError, match="已恢复发布前"):
        publish(project, web_only=True)
    assert {name: (project.web / name).read_bytes() for name in release.STATIC_FILES if (project.web / name).exists()} == previous
    assert (project.install / "data" / "config.json").read_bytes() == b"synthetic-config-must-stay"


def test_rollback_failure_reports_backup_and_preserves_original_failure(project, monkeypatch):
    real_replace = release.atomic_replace
    attempts = []

    def fail_publish_and_one_restore(source, target):
        name = Path(target).name
        attempts.append(name)
        if name == "index.html":
            raise OSError("Synthetic original publication error")
        if name == "app.js" and attempts.count("app.js") == 2:
            raise PermissionError("Synthetic rollback error")
        real_replace(source, target)

    monkeypatch.setattr(release, "atomic_replace", fail_publish_and_one_restore)
    with pytest.raises(release.ReleaseError, match="部分文件恢复未完成") as error:
        publish(project, web_only=True)
    assert str(error.value.__cause__) == "Synthetic original publication error"
    assert "备份" in str(error.value) and "versions" in str(error.value)
    assert attempts.count("index.html") == 1 and "manifest.webmanifest" not in attempts
    assert (project.web / "manifest.webmanifest").read_bytes() == project.old["manifest.webmanifest"]
    assert (project.web / "app.css").read_bytes() == project.old["app.css"]
    assert (project.web / "icon.svg").read_bytes() == project.old["icon.svg"]
    backups = list((project.install / "versions").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "mobile_web" / "app.js").read_bytes() == project.old["app.js"]


def test_running_target_stops_default_release_before_any_build_or_static_write(project, monkeypatch):
    monkeypatch.setattr(release, "running_process_ids", lambda executable: [12345])
    calls = []
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(release.ReleaseError, match="正常关闭"):
        publish(project)
    assert calls == []
    assert not (project.install / "versions").exists()
    assert (project.web / "manifest.webmanifest").read_bytes() == project.old["manifest.webmanifest"]


def test_skip_build_installs_existing_exe_and_preserves_previous_binary(project, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Skip-build ran PyInstaller")

    monkeypatch.setattr(release.subprocess, "run", unexpected)
    result = publish(project, skip_build=True)
    assert result["desktop_updated"] and result["web_updated"]
    assert (project.install / "ClaudeBridge.exe").read_bytes() == b"new-desktop"
    assert (Path(result["backup"]) / "ClaudeBridge.exe").read_bytes() == b"old-desktop"


def test_default_build_uses_existing_spec_with_current_python(project, monkeypatch):
    calls = []

    def build(command, **kwargs):
        calls.append((command, kwargs))
        (project.root / "dist" / "ClaudeBridge.exe").write_bytes(b"freshly-built-desktop")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(release.subprocess, "run", build)
    result = publish(project)
    assert result["desktop_updated"]
    assert calls == [([sys.executable, "-m", "PyInstaller", "--noconfirm", str(project.root / "ClaudeBridge.spec")],
                      {"cwd": project.root, "check": True})]
    assert (project.install / "ClaudeBridge.exe").read_bytes() == b"freshly-built-desktop"


def test_desktop_install_failure_does_not_publish_web(project, monkeypatch):
    real_replace = release.atomic_replace

    def locked_exe(source, target):
        if Path(target).name == "ClaudeBridge.exe":
            raise PermissionError("Synthetic in-use executable")
        real_replace(source, target)

    monkeypatch.setattr(release, "atomic_replace", locked_exe)
    with pytest.raises(release.ReleaseError, match="手机网页尚未更新"):
        publish(project, skip_build=True)
    assert (project.install / "ClaudeBridge.exe").read_bytes() == b"old-desktop"
    assert (project.web / "manifest.webmanifest").read_bytes() == project.old["manifest.webmanifest"]


@pytest.mark.skipif(os.name != "nt", reason="Windows process query path comparison")
def test_process_guard_matches_full_executable_path_only(project, monkeypatch):
    rows = [{"ProcessId": 101, "ExecutablePath": str(project.install / "ClaudeBridge.exe")},
            {"ProcessId": 102, "ExecutablePath": str(project.root / "other" / "ClaudeBridge.exe")}]
    monkeypatch.setattr(release, "running_process_ids", RUNNING_PROCESS_IDS)
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, json.dumps(rows), ""))
    assert release.running_process_ids(project.install / "ClaudeBridge.exe") == [101]


def test_release_lock_rejects_parallel_publisher_and_can_reopen(project):
    with release.release_lock(project.install):
        with pytest.raises(release.ReleaseError, match="另一个"):
            with release.release_lock(project.install):
                raise AssertionError("Parallel publisher acquired the lock")
    with release.release_lock(project.install):
        pass


def test_web_destination_cannot_be_user_data(project):
    with pytest.raises(release.ReleaseError, match="不能位于 data"):
        publish(project, web_only=True, web_dir=project.install / "data" / "mobile_web")
    assert (project.install / "data" / "config.json").read_bytes() == b"synthetic-config-must-stay"


def test_publisher_cannot_overwrite_source_web_files(project):
    with pytest.raises(release.ReleaseError, match="源目录和发布目录必须分开"):
        publish(project, web_only=True, web_dir=project.source)
    assert (project.source / "app.js").read_bytes() == project.payloads["app.js"]


def test_cli_supports_isolated_source_and_destination(project, capsys):
    assert release.main(["--web-only", "--source-web-dir", str(project.source),
                         "--install-dir", str(project.install), "--web-dir", str(project.web)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["web_updated"] and not result["desktop_updated"]
    first = result["version"]
    (project.source / "app.css").write_bytes(b"/* Synthetic second version */")
    assert release.main(["--web-only", "--source-web-dir", str(project.source),
                         "--install-dir", str(project.install), "--web-dir", str(project.web)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["version"] != first
