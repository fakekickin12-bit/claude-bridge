"""Audit publishable source or a ZIP without printing matched secret values.

By default, Git-tracked and non-ignored candidate files are checked. Before Git is
initialized, every file except Git internals is checked. Private indicators can
be supplied in a newline-separated file OUTSIDE the public checkout, or through
CLAUDE_BRIDGE_AUDIT_PRIVATE_PATTERNS. A binary release still must be built from
    audited source: scanning raw ZIP bytes cannot inspect a compressed Python bundle.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import zipfile


DENIED_PARTS = {"data", "qa", "build", "dist", "versions", ".ssh", "__pycache__",
                ".pytest_cache", ".venv", "venv", "node_modules"}
DENIED_NAMES = {"auth.json", "access.json", "config.json", "config.toml", "known_hosts",
                "credentials.json", "secrets.json", "token.json", "tokens.json", "keys.json",
                "draft.json", "models_cache.json", "id_rsa", "id_ed25519", "tunnel_ed25519", "settings.json"}
DENIED_SUFFIXES = {".pem", ".key", ".pub", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db",
                   ".log", ".pyc", ".pyo", ".exe", ".dll", ".pyd", ".so", ".dylib", ".zip", ".7z", ".rar", ".gz",
                   ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".mp3", ".wav"}
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "api-token": re.compile(r"(?<![\w-])(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|"
                            r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                            r"xox[baprs]-[A-Za-z0-9-]{20,}|AIza[A-Za-z0-9_-]{30,}|"
                            r"AKIA[A-Z0-9]{16}|glpat-[A-Za-z0-9_-]{20,})(?![\w-])"),
    "pairing-code": re.compile(r"cb-pair-[A-Za-z0-9_-]{16,}"),
    "encrypted-credential": re.compile(r"[\"']key_encrypted[\"']\s*:\s*[\"']([^\"']{40,})[\"']"),
    "credential-in-url": re.compile(r"https?://[^\s/:@]+:[^\s/@]+@"),
    "personal-home-path": re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+|/" + r"home/[^/\s\"']+/)"),
}
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
PRIVATE_NETS = tuple(ipaddress.ip_network(".".join(map(str, octets)) + "/" + str(prefix))
                     for octets, prefix in (((10, 0, 0, 0), 8), ((172, 16, 0, 0), 12), ((192, 168, 0, 0), 16)))
MAX_FILE = 256 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def portable_binary(name):
    """Allow only a named app and its onedir DLL/PYD/standard-library ZIP layout."""
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if parts and parts[0] == "ClaudeBridge":
        parts = parts[1:]
    if parts == ("ClaudeBridge.exe",):
        return True
    return bool(len(parts) >= 2 and parts[0] == "_internal"
                and (PurePosixPath(parts[-1]).suffix.casefold() in {".dll", ".pyd"}
                     or parts == ("_internal", "base_library.zip")))


def path_findings(name, *, allow_executable=False, portable=False):
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    lower = [part.casefold() for part in path.parts]
    rules = []
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", normalized):
        rules.append("unsafe-path")
    if any(part in DENIED_PARTS for part in lower):
        rules.append("runtime-directory")
    basename = path.name.casefold()
    if basename in DENIED_NAMES or basename.startswith(".env") and basename not in {".env.example", ".env.sample"}:
        rules.append("credential-config-file")
    if any(word in basename for word in ("配对信息", "网页登录信息")):
        rules.append("credential-note")
    if path.suffix.casefold() in DENIED_SUFFIXES and not (
            portable and portable_binary(name)
            or allow_executable and path.name == "ClaudeBridge.exe" and len(path.parts) <= 2):
        rules.append("binary-or-runtime-file")
    if any(PATTERNS[rule].search(normalized) for rule in ("api-token", "pairing-code", "private-key")):
        rules.append("credential-in-filename")
    return [Finding(normalized, 0, rule) for rule in rules]


def synthetic_token(value):
    """Permit clearly labelled fake test values, never arbitrary high-entropy keys."""
    marker = r"(?:synthetic|fake|example)[-_][A-Za-z_-]+"
    return bool(re.fullmatch(r"(?:sk-(?:proj-|svcacct-)?|gh[pousr]_|cb-pair-)?" + marker, value, re.I)
                or re.fullmatch(r"https?://" + marker + ":" + marker + "@", value, re.I))


def content_findings(name, content, private_indicators=(), *, binary=False):
    findings = []
    text = content.decode("utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in PATTERNS.items():
            if binary and rule != "api-token":
                continue  # Upstream PE libraries contain format markers and build-system paths.
            if any(not synthetic_token(match.group(1) if rule == "encrypted-credential" else match.group())
                   for match in pattern.finditer(line)):
                findings.append(Finding(name, number, rule))
        for match in (() if binary else IPV4.finditer(line)):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if any(address in network for network in PRIVATE_NETS):
                findings.append(Finding(name, number, "private-network-address"))
                break
        if any(indicator.casefold() in line.casefold() for indicator in private_indicators if indicator):
            findings.append(Finding(name, number, "private-indicator"))
    return findings


def candidate_paths(root, *, tracked_only=False):
    arguments = ["git", "-C", str(root), "ls-files", "--cached", "-z"]
    if not tracked_only:
        arguments += ["--others", "--exclude-standard"]
    result = subprocess.run(arguments, capture_output=True)
    if result.returncode == 0:
        return sorted({name for name in result.stdout.decode("utf-8").split("\0") if name})
    if tracked_only:
        raise ValueError("--tracked requires a Git checkout")
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                  if (path.is_file() or path.is_symlink()) and ".git" not in path.relative_to(root).parts)


def audit_source(root, *, private_indicators=(), tracked_only=False):
    findings, count = [], 0
    for name in candidate_paths(root, tracked_only=tracked_only):
        count += 1
        findings.extend(path_findings(name))
        target = root / name
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            findings.append(Finding(name, 0, "symlink-or-outside-root"))
        elif not target.is_file() or target.stat().st_size > MAX_FILE:
            findings.append(Finding(name, 0, "missing-or-oversized-file"))
        else:
            findings.extend(content_findings(name, target.read_bytes(), private_indicators))
    return count, findings


def audit_archive(archive, *, private_indicators=(), allow_executable=False, portable=False):
    findings, count, seen = [], 0, set()
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist()) > 10000 or sum(info.file_size for info in bundle.infolist()) > 512 * 1024 * 1024:
            raise ValueError("Archive has too many entries or exceeds audit size limits")
        for entry in bundle.infolist():
            if entry.is_dir():
                continue
            count += 1
            name = entry.filename
            findings.extend(path_findings(name, allow_executable=allow_executable, portable=portable))
            if name.casefold() in seen:
                findings.append(Finding(name, 0, "duplicate-archive-entry"))
            seen.add(name.casefold())
            if stat.S_ISLNK(entry.external_attr >> 16):
                findings.append(Finding(name, 0, "symlink-or-outside-root"))
            if entry.file_size > MAX_FILE:
                findings.append(Finding(name, 0, "missing-or-oversized-file"))
            else:
                findings.extend(content_findings(name, bundle.read(entry), private_indicators,
                                                 binary=portable and portable_binary(name)))
    return count, findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tracked", action="store_true", help="Check Git-tracked source only")
    parser.add_argument("--archive", type=Path, help="Check a source or portable release ZIP instead")
    parser.add_argument("--allow-executable", action="store_true", help="Allow only the named portable executable in a ZIP")
    parser.add_argument("--portable", action="store_true",
                        help="Allow only ClaudeBridge.exe plus _internal DLL/PYD/stdlib ZIP runtime files; does not inspect compressed Python contents")
    parser.add_argument("--private-patterns-file", "--private-patterns", type=Path,
                        help="Newline-separated private indicators in a file outside the public checkout")
    args = parser.parse_args(argv)
    indicators = os.environ.get("CLAUDE_BRIDGE_AUDIT_PRIVATE_PATTERNS", "").splitlines()
    if args.private_patterns_file:
        if args.private_patterns_file.resolve().is_relative_to(args.root.resolve()):
            parser.error("The private indicator file must remain outside the public checkout")
        indicators += args.private_patterns_file.read_text(encoding="utf-8").splitlines()
    indicators = [line.strip() for line in indicators if line.strip()]
    try:
        if args.archive:
            count, findings = audit_archive(args.archive, private_indicators=indicators,
                                            allow_executable=args.allow_executable, portable=args.portable)
        else:
            count, findings = audit_source(args.root, private_indicators=indicators, tracked_only=args.tracked)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__}))
        return 2
    reported = []
    for finding in findings:
        item = asdict(finding)
        for pattern in PATTERNS.values():
            item["path"] = pattern.sub("<redacted>", item["path"])
        for indicator in indicators:
            item["path"] = re.sub(re.escape(indicator), "<redacted>", item["path"], flags=re.I)
        reported.append(item)
    print(json.dumps({"ok": not findings, "checked_files": count,
                      "findings": reported}, ensure_ascii=False, indent=2))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
