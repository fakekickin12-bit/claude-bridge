"""Publication checks use disposable canaries and never inspect real user secrets."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import zipfile
import stat


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "audit_public.py"
spec = importlib.util.spec_from_file_location("public_audit", SOURCE)
audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)


def test_secret_detection_never_prints_canary_value(tmp_path):
    canary = "sk-" + "8a4c" * 12
    (tmp_path / "safe.py").write_text("value = " + repr(canary), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SOURCE), "--root", str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 1 and '"api-token"' in result.stdout
    assert canary not in result.stdout + result.stderr
    (tmp_path / (canary + ".txt")).write_text("also check filenames", encoding="utf-8")
    result = subprocess.run([sys.executable, str(SOURCE), "--root", str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8")
    assert "credential-in-filename" in result.stdout and canary not in result.stdout + result.stderr


def test_runtime_data_and_symlinks_are_not_publishable():
    assert audit.path_findings("data/config.json")
    assert audit.path_findings("qa/example.png")
    assert audit.path_findings("private/identity.key")
    assert audit.path_findings(".env")
    assert not audit.path_findings(".env.example")
    assert not audit.path_findings("ClaudeBridge.spec")


def test_private_indicator_file_is_external_and_values_are_redacted(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    indicator = "internal-host-" + "846204"
    (checkout / "example.md").write_text(indicator, encoding="utf-8")
    private = tmp_path / "private-indicators.txt"
    private.write_text(indicator, encoding="utf-8")
    result = subprocess.run([sys.executable, str(SOURCE), "--root", str(checkout),
                             "--private-patterns-file", str(private)], capture_output=True, text=True)
    assert result.returncode == 1 and "private-indicator" in result.stdout
    assert indicator not in result.stdout + result.stderr
    result = subprocess.run([sys.executable, str(SOURCE), "--root", str(checkout),
                             "--private-patterns-file", str(checkout / "example.md")],
                            capture_output=True, text=True)
    assert result.returncode == 2


def test_git_ignored_local_data_is_not_a_candidate_but_tracked_data_is(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("data/\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "safe.py").write_text("print('safe')", encoding="utf-8")
    count, findings = audit.audit_source(tmp_path)
    assert count == 2 and not findings
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "data/config.json"], check=True)
    _, findings = audit.audit_source(tmp_path)
    assert any(item.rule == "runtime-directory" for item in findings)


def test_archive_content_and_executable_allowance(tmp_path):
    bundle = tmp_path / "release.zip"
    canary = "ghp_" + "B2c9" * 12
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("ClaudeBridge/ClaudeBridge.exe", b"synthetic executable")
        archive.writestr("ClaudeBridge/README.md", "safe text")
    assert audit.audit_archive(bundle)[1]
    assert not audit.audit_archive(bundle, allow_executable=True)[1]
    with zipfile.ZipFile(bundle, "a") as archive:
        archive.writestr("../escape.txt", "unsafe")
        archive.writestr("ClaudeBridge/notes.txt", canary)
    _, findings = audit.audit_archive(bundle, allow_executable=True)
    assert any(item.rule == "unsafe-path" for item in findings)
    assert any(item.rule == "api-token" for item in findings)


def test_portable_archive_allows_only_expected_runtime_layout_and_redacts_indicators(tmp_path):
    bundle = tmp_path / "portable.zip"
    # Upstream binary strings alone are not evidence of a bundled private key.
    upstream_marker = ("-----BEGIN " + "PRIVATE KEY-----").encode()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("ClaudeBridge/ClaudeBridge.exe", upstream_marker)
        archive.writestr("ClaudeBridge/_internal/python.dll", upstream_marker)
        archive.writestr("ClaudeBridge/_internal/package/_module.pyd", b"synthetic binary")
        archive.writestr("ClaudeBridge/_internal/base_library.zip", b"synthetic compressed archive")
        archive.writestr("ClaudeBridge/README.md", "safe text")
    assert not audit.audit_archive(bundle, portable=True)[1]
    indicator = "private-build-" + "582963"
    with zipfile.ZipFile(bundle, "a") as archive:
        archive.writestr("ClaudeBridge/_internal/data/config.json", "{}")
        archive.writestr("ClaudeBridge/_internal/known_hosts", "not publishable")
        archive.writestr("ClaudeBridge/unexpected.dll", b"outside runtime")
        archive.writestr("ClaudeBridge/_internal/unexpected.zip", b"outside known standard-library ZIP")
        archive.writestr("ClaudeBridge/_internal/extra.dll", indicator)
        link = zipfile.ZipInfo("ClaudeBridge/_internal/link.dll")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "outside")
    _, findings = audit.audit_archive(bundle, portable=True, private_indicators=[indicator])
    rules = {item.rule for item in findings}
    assert {"runtime-directory", "credential-config-file", "binary-or-runtime-file",
            "private-indicator", "symlink-or-outside-root"} <= rules
    canary = ("sk-" + "8a3f" * 12).encode()
    assert any(item.rule == "api-token" for item in audit.content_findings("_internal/test.dll", canary, binary=True))


def test_documentation_and_fake_keys_are_allowed_but_private_network_is_flagged():
    fake = ("sk-" + "synthetic-" + "not-a-real-key-value").encode()
    assert not audit.content_findings("test.py", fake)
    assert not audit.content_findings("readme.md", b"https://api.example.test 127.0.0.1 203.0.113.10")
    address = ".".join(["192", "168", "44", "10"])
    assert any(item.rule == "private-network-address" for item in audit.content_findings("config.txt", address.encode()))
    looks_fake_but_has_entropy = ("sk-" + "example-" + "a38f" * 10).encode()
    assert any(item.rule == "api-token" for item in audit.content_findings("config.txt", looks_fake_but_has_entropy))
