"""Assemble a clean Windows folder release with notices and optional web sources."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import metadata
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_bridge import static_release

LICENSE_URLS = {
    "LGPL-3.0.txt": "https://www.gnu.org/licenses/lgpl-3.0.txt",
    "GPL-3.0.txt": "https://www.gnu.org/licenses/gpl-3.0.txt",
}
BACKEND = ("core.py", "device_auth.py", "media.py", "mobile_service.py", "reply_translations.py",
           "remote.py", "desktop_remote.py", "requirements-mobile.txt")
DOCS = ("README.md", "LICENSE", "SECURITY.md", "THIRD_PARTY_NOTICES.md")


def dependency_notices(executable):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    embedded = next(name for name in archive.toc if name.endswith(".pyz"))
    prefixes = {name.split(".")[0] for name in archive.open_embedded_archive(embedded).toc}
    distribution_map = metadata.packages_distributions()
    names = {name for prefix in prefixes for name in distribution_map.get(prefix, [])}
    names.add("PyInstaller")
    versions, notices = {}, {}
    for name in sorted(names):
        distribution = metadata.distribution(name)
        versions[name] = distribution.version
        for file in distribution.files or []:
            if not any(part in str(file).casefold() for part in ("license", "copying", "notice", "copyright")):
                continue
            original = Path(distribution.locate_file(file))
            if original.is_file() and original.stat().st_size < 4_000_000:
                notices[f"LICENSES/{name}/{Path(str(file)).name}"] = original.read_bytes()
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python runtime license text is missing")
    notices["LICENSES/Python-LICENSE.txt"] = python_license.read_bytes()
    for name, url in LICENSE_URLS.items():
        request = urllib.request.Request(url, headers={"User-Agent": "ClaudeBridge-release-builder"})
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read(100_000)
        if b"GNU" not in text or b"<html" in text.lower():
            raise RuntimeError("License download is not the expected plain text: " + name)
        notices["LICENSES/" + name] = text
    return versions, notices


def main():
    application = ROOT / "dist/ClaudeBridge"
    executable = application / "ClaudeBridge.exe"
    internal = application / "_internal"
    if not executable.is_file() or not internal.is_dir():
        raise RuntimeError("Build ClaudeBridgePortable.spec first")
    entries = {"ClaudeBridge.exe": executable.read_bytes()}
    for file in internal.rglob("*"):
        if file.is_symlink():
            raise RuntimeError("Do not package symlinks")
        if file.is_file():
            entries[file.relative_to(application).as_posix()] = file.read_bytes()
    for name in DOCS:
        entries[name] = (ROOT / name).read_bytes()
    for name in BACKEND:
        entries["mobile-service/" + name] = (ROOT / name).read_bytes()
    for file in (ROOT / "deploy_mobile").iterdir():
        if file.is_file() and file.suffix in {".py", ".md", ".json", ".bridge"}:
            entries["mobile-service/deploy_mobile/" + file.name] = file.read_bytes()
    web_version, web_files = static_release(ROOT / "mobile_web")
    for name, content in web_files.items():
        entries["mobile-service/mobile_web/" + name] = content
    dependencies, notices = dependency_notices(executable)
    entries.update(notices)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    info = {"application": "Claude Bridge", "source": "https://github.com/fakekickin12-bit/claude-bridge",
            "source_commit": commit.stdout.strip() if commit.returncode == 0 else "uncommitted",
            "built_at": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
            "dependencies": dependencies, "web_version": web_version,
            "credentials_included": False, "configuration": "Create your own data folder on first use",
            "license_sources": LICENSE_URLS}
    entries["BUILD-INFO.json"] = (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode()
    entries["FILE-SHA256SUMS.txt"] = "".join(
        hashlib.sha256(content).hexdigest() + "  " + name + "\n" for name, content in sorted(entries.items())).encode()
    output = ROOT / "release"
    output.mkdir(exist_ok=True)
    archive = output / "ClaudeBridge-Windows.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for name, content in sorted(entries.items()):
            if "data" in Path(name).parts or ".." in Path(name).parts:
                raise RuntimeError("Runtime data cannot be packaged")
            bundle.writestr("ClaudeBridge/" + name, content)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(digest + "  " + archive.name + "\n", encoding="ascii")
    print(json.dumps({"archive": str(archive), "files": len(entries), "sha256": digest,
                      "bytes": archive.stat().st_size, "dependencies": dependencies}))


if __name__ == "__main__":
    main()
