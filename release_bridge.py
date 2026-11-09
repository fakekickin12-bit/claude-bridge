"""Build the desktop app and publish its matching mobile static files."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_INSTALL_DIR = Path(os.environ.get("CLAUDE_BRIDGE_INSTALL_DIR", str(
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "ClaudeBridge"))).expanduser()
STATIC_FILES = ("app.js", "app.css", "icon.svg", "desktop.js", "desktop.css", "desktop.html", "index.html", "manifest.webmanifest")
BUILD_TOKEN = b"__BRIDGE_BUILD__"


class ReleaseError(Exception):
    pass


def file_bytes(path):
    if path.is_symlink():
        raise ReleaseError(f"发布文件不能是符号链接：{path}")
    return path.read_bytes()


def static_release(source_dir):
    source_dir = Path(source_dir)
    files = {name: file_bytes(source_dir / name) for name in STATIC_FILES}
    if files["app.js"].count(BUILD_TOKEN) != 1:
        raise ReleaseError("mobile_web/app.js 必须包含一个 __BRIDGE_BUILD__ 版本占位符。")
    try:
        manifest = json.loads(files["manifest.webmanifest"])
    except (ValueError, UnicodeError) as error:
        raise ReleaseError("手机网页的 manifest.webmanifest 不是有效 JSON。") from error
    if not isinstance(manifest, dict):
        raise ReleaseError("手机网页的 manifest.webmanifest 必须是 JSON 对象。")
    manifest.pop("bridge_version", None)
    files["manifest.webmanifest"] = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256()
    for name in STATIC_FILES:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(files[name] + b"\0")
    version = digest.hexdigest()
    files = {name: value.replace(BUILD_TOKEN, version.encode("ascii")) for name, value in files.items()}
    manifest["bridge_version"] = version
    files["manifest.webmanifest"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    return version, files


def running_process_ids(executable):
    target = os.path.normcase(str(Path(executable).resolve()))
    if os.name == "nt":
        query = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                 "Get-CimInstance Win32_Process -Filter \"Name='ClaudeBridge.exe'\" | "
                 "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress")
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", query],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
        if result.returncode:
            raise ReleaseError("无法检查 Claude Bridge 是否正在运行，已停止安装。")
        try:
            rows = json.loads(result.stdout.strip() or "[]")
        except ValueError as error:
            raise ReleaseError("进程检查未返回有效结果，已停止安装。") from error
        rows = [rows] if isinstance(rows, dict) else rows
        found = []
        for row in rows or []:
            path = row.get("ExecutablePath")
            if not path:
                raise ReleaseError("无法核实同名 Claude Bridge 进程的路径，已停止安装。")
            if os.path.normcase(str(Path(path).resolve())) == target:
                found.append(int(row["ProcessId"]))
        return found
    process_root = Path("/proc")
    if not process_root.is_dir():
        raise ReleaseError("当前系统无法核实桌面进程，已停止安装。")
    found = []
    for process in process_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if str((process / "exe").resolve(strict=True)) == target:
                found.append(int(process.name))
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return found


def ensure_desktop_closed(executable):
    if running_process_ids(executable):
        raise ReleaseError("Claude Bridge 正在运行。请正常关闭窗口、保存草稿后再发布。")


@contextmanager
def release_lock(install_dir):
    install_dir.mkdir(parents=True, exist_ok=True)
    handle = (install_dir / ".release.lock").open("a+b")
    try:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ReleaseError("另一个 Claude Bridge 发布正在进行，请等它完成。") from error
        yield
    finally:
        handle.close()


def existing_files(web_dir, files):
    return {name: file_bytes(web_dir / name) for name in files if (web_dir / name).exists()}


def backup_files(install_dir, version, previous_web, desktop=None):
    if not previous_web and desktop is None:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = install_dir / "versions" / f"{stamp}-{version[:12]}"
    backup.mkdir(parents=True, exist_ok=False)
    if desktop is not None:
        (backup / "ClaudeBridge.exe").write_bytes(desktop)
    if previous_web:
        directory = backup / "mobile_web"
        directory.mkdir()
        for name, content in previous_web.items():
            (directory / name).write_bytes(content)
    return backup


def staged_file(directory, content, suffix):
    handle, path = tempfile.mkstemp(dir=directory, prefix=".bridge-release-", suffix=suffix)
    temporary = Path(path)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        return temporary
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_replace(source, target):
    os.replace(source, target)


def install_desktop(content, target):
    ensure_desktop_closed(target)
    temporary = staged_file(target.parent, content, ".exe")
    try:
        atomic_replace(temporary, target)
    except OSError as error:
        raise ReleaseError("桌面端安装未完成。请正常关闭 Claude Bridge 后重新发布；手机网页尚未更新。") from error
    finally:
        temporary.unlink(missing_ok=True)


def restore_web_files(web_dir, previous, replaced):
    failed = []
    for name in reversed(replaced):
        temporary = None
        try:
            if name in previous:
                temporary = staged_file(web_dir, previous[name], Path(name).suffix)
                atomic_replace(temporary, web_dir / name)
            else:
                (web_dir / name).unlink(missing_ok=True)
        except Exception:
            failed.append(name)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return failed


def publish_web(web_dir, files, previous=None, backup=None):
    web_dir.mkdir(parents=True, exist_ok=True)
    previous = existing_files(web_dir, files) if previous is None else previous
    staged = {}
    replaced = []
    try:
        for name, content in files.items():
            staged[name] = staged_file(web_dir, content, Path(name).suffix)
        order = [name for name in STATIC_FILES if name not in {"index.html", "manifest.webmanifest"}]
        order.extend(["index.html", "manifest.webmanifest"])
        for name in order:
            atomic_replace(staged[name], web_dir / name)
            replaced.append(name)
    except Exception as error:
        failed = restore_web_files(web_dir, previous, replaced)
        message = "手机网页发布未完成，版本清单未更新。"
        if failed:
            message += "部分文件恢复未完成：" + "、".join(failed) + "。"
            if backup is not None:
                message += f"可从备份 {backup} 恢复。"
        else:
            message += "已恢复发布前的静态文件。"
        raise ReleaseError(message + "请修复后重新执行 --web-only。") from error
    finally:
        for temporary in staged.values():
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def release(project_dir=PROJECT_DIR, *, install_dir=DEFAULT_INSTALL_DIR, web_dir=None, source_web_dir=None,
            web_only=False, skip_build=False):
    project_dir, install_dir = Path(project_dir), Path(install_dir)
    web_dir = Path(web_dir) if web_dir is not None else install_dir / "mobile-service" / "mobile_web"
    source_web_dir = Path(source_web_dir) if source_web_dir is not None else project_dir / "mobile_web"
    if source_web_dir.resolve() == web_dir.resolve():
        raise ReleaseError("手机网页源目录和发布目录必须分开。")
    for directory in (install_dir / "data", project_dir / "data", DEFAULT_INSTALL_DIR / "data"):
        if web_dir.resolve().is_relative_to(directory.resolve()):
            raise ReleaseError("手机网页发布目录不能位于 data 中。")
    desktop_target = install_dir / "ClaudeBridge.exe"
    if not web_only:
        ensure_desktop_closed(desktop_target)
    with release_lock(install_dir):
        version, files = static_release(source_web_dir)
        if not web_only and not skip_build:
            specification = project_dir / "ClaudeBridge.spec"
            if not specification.is_file():
                raise ReleaseError("未找到 ClaudeBridge.spec，无法构建桌面端。")
            subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", str(specification)],
                           cwd=project_dir, check=True)
        if not web_only:
            ensure_desktop_closed(desktop_target)
        desktop_source = project_dir / "dist" / "ClaudeBridge.exe"
        new_desktop = file_bytes(desktop_source) if not web_only else None
        previous_desktop = file_bytes(desktop_target) if not web_only and desktop_target.exists() else None
        desktop_changed = not web_only and new_desktop != previous_desktop
        previous_web = existing_files(web_dir, files)
        web_changed = previous_web != files
        backup = backup_files(install_dir, version, previous_web if web_changed else {},
                              previous_desktop if desktop_changed else None)
        if desktop_changed:
            install_desktop(new_desktop, desktop_target)
        if web_changed:
            publish_web(web_dir, files, previous_web, backup)
        return {"version": version, "desktop_updated": desktop_changed, "web_updated": web_changed,
                "backup": str(backup) if backup else None, "web_directory": str(web_dir)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="统一构建桌面端并发布手机网页。")
    parser.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR)
    parser.add_argument("--web-dir", type=Path)
    parser.add_argument("--source-web-dir", type=Path, help="指定待发布手机网页源目录")
    parser.add_argument("--web-only", action="store_true", help="只发布手机网页静态文件")
    parser.add_argument("--skip-build", action="store_true", help="使用已有 dist/ClaudeBridge.exe")
    args = parser.parse_args(argv)
    try:
        result = release(install_dir=args.install_dir, web_dir=args.web_dir, source_web_dir=args.source_web_dir,
                         web_only=args.web_only, skip_build=args.skip_build)
    except (ReleaseError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
