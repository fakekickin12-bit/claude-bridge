"""VPS actions invoked by vps_deploy.py only after its explicit apply flag."""
from pathlib import Path
import base64
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

TUNNEL_USER = "claude-bridge-tunnel"
TUNNEL_HOME = Path("/var/lib/claude-bridge-tunnel")
SSH_FILE = Path("/etc/ssh/sshd_config.d/60-claude-bridge-tunnel.conf")
CADDY_FILE = Path("/etc/caddy/Caddyfile")
SITE_FILE = Path("/etc/caddy/claude-bridge/bridge.caddy")
SSH_RULES = """# Claude Bridge dedicated reverse-forward identity.
Match User claude-bridge-tunnel
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding remote
    AllowStreamLocalForwarding no
    GatewayPorts no
    PermitListen 127.0.0.1:18770
    PermitOpen none
    X11Forwarding no
    AllowAgentForwarding no
    PermitTTY no
    PermitTunnel no
    PermitUserRC no
    MaxSessions 0
    ClientAliveInterval 15
    ClientAliveCountMax 2
Match all
"""


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=180, **kwargs)


def atomic(path, text, mode=None, owner=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("A managed configuration path is a symlink; review it manually")
    existing = path.stat() if path.exists() else None
    permissions = mode
    if permissions is None:
        permissions = stat.S_IMODE(existing.st_mode) if existing else 0o644
    ownership = owner
    if ownership is None:
        ownership = (existing.st_uid, existing.st_gid) if existing else (0, 0)
    payload = text.encode("utf-8") if isinstance(text, str) else text
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        os.chmod(temporary, permissions)
        os.chown(temporary, *ownership)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot(path):
    if path.is_symlink():
        raise RuntimeError("A managed configuration path is a symlink; review it manually")
    if path.exists():
        metadata = path.stat()
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns())
        backup = Path("/root/claude-bridge-backups") / stamp / path.relative_to("/")
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, backup)
        return {"data": path.read_bytes(), "mode": stat.S_IMODE(metadata.st_mode),
                "uid": metadata.st_uid, "gid": metadata.st_gid}
    return None


def restore(path, previous):
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic(path, previous["data"], previous["mode"], (previous["uid"], previous["gid"]))


def tunnel(public_key):
    fields = public_key.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519" or len(fields[1]) > 200:
        raise RuntimeError("Expected one ed25519 public key")
    base64.b64decode(fields[1], validate=True)
    if "\n" in public_key.strip() or "\r" in public_key:
        raise RuntimeError("Expected one public-key line")
    try:
        account = pwd.getpwnam(TUNNEL_USER)
        if Path(account.pw_dir) != TUNNEL_HOME or account.pw_shell != "/usr/sbin/nologin" or account.pw_uid == 0:
            raise RuntimeError("Existing tunnel account does not match the dedicated account definition")
    except KeyError:
        command(["useradd", "--system", "--create-home", "--home-dir", str(TUNNEL_HOME), "--shell", "/usr/sbin/nologin", TUNNEL_USER])
        # A non-password '*' value avoids a locked-account '!' test while every
        # authentication mechanism except public key is disabled for this user.
        command(["usermod", "--password", "*", TUNNEL_USER])
        account = pwd.getpwnam(TUNNEL_USER)
    ssh_directory = TUNNEL_HOME / ".ssh"
    ssh_directory.mkdir(mode=0o700, exist_ok=True)
    os.chown(ssh_directory, account.pw_uid, account.pw_gid)
    os.chmod(ssh_directory, 0o700)
    authorized = ssh_directory / "authorized_keys"
    old_key = snapshot(authorized)
    old_rules = snapshot(SSH_FILE)
    keyline = 'restrict,port-forwarding,permitlisten="127.0.0.1:18770" ' + " ".join(fields[:2]) + " claude-bridge-tunnel\n"
    try:
        atomic(authorized, keyline, 0o600)
        os.chown(authorized, account.pw_uid, account.pw_gid)
        atomic(SSH_FILE, SSH_RULES)
        command(["/usr/sbin/sshd", "-t"])
        effective = dict(line.partition(" ")[::2] for line in command([
            "/usr/sbin/sshd", "-T", "-C", "user=" + TUNNEL_USER + ",host=localhost,addr=127.0.0.1"]).stdout.splitlines())
        expected = {"gatewayports": "no", "allowtcpforwarding": "remote", "permitlisten": "127.0.0.1:18770",
                    "permitopen": "none", "permittty": "no", "maxsessions": "0", "passwordauthentication": "no",
                    "kbdinteractiveauthentication": "no", "x11forwarding": "no", "allowagentforwarding": "no",
                    "allowstreamlocalforwarding": "no", "authenticationmethods": "publickey", "permituserrc": "no",
                    "clientaliveinterval": "15", "clientalivecountmax": "2"}
        if any(effective.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Earlier sshd settings override the restricted identity; no SSH reload performed")
        command(["systemctl", "reload", "ssh"])
    except Exception:
        restore(SSH_FILE, old_rules)
        restore(authorized, old_key)
        raise
    return {"configured": "restricted_tunnel", "user": TUNNEL_USER, "listen": "127.0.0.1:18770"}


def caddy(site, domain):
    if (not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", domain)
            or domain == "example.com" or domain.endswith(".example.com")
            or not site.startswith(domain + " {") or "0.0.0.0:18770" in site):
        raise RuntimeError("Unexpected Caddy site definition")
    # Refuse to compete with an existing web service. Do not inspect or modify
    # any other listener or independently managed service.
    listeners = command(["ss", "-H", "-ltnp", "( sport = :80 or sport = :443 )"]).stdout.splitlines()
    if any('"caddy"' not in line for line in listeners):
        raise RuntimeError("TCP 80/443 already has a different listener")
    original = snapshot(CADDY_FILE)
    original_site = snapshot(SITE_FILE)
    if original_site is not None and not original_site["data"].decode("utf-8").startswith(domain + " {"):
        raise RuntimeError("The dedicated site path already contains another configuration; review it manually")
    installed = bool(shutil.which("caddy"))
    was_running = subprocess.run(["systemctl", "is-active", "--quiet", "caddy"], capture_output=True).returncode == 0
    enabled_result = subprocess.run(["systemctl", "is-enabled", "caddy"], capture_output=True, text=True)
    was_enabled = enabled_result.stdout.strip()
    if installed and was_enabled not in {"enabled", "enabled-runtime", "disabled"}:
        raise RuntimeError("Existing Caddy enablement requires manual deployment review")
    if not installed and original is not None:
        raise RuntimeError("Caddyfile already exists without a Caddy binary; preserve it and review package installation manually")
    fresh_install = False
    managed_configuration = installed

    def rollback_step(label, operation):
        try:
            operation()
        except Exception as cleanup_error:
            # Keep attempting the other rollback steps and preserve the original
            # deployment exception as the final error reported to the caller.
            print("Caddy rollback failed during " + label + ": " + type(cleanup_error).__name__, file=sys.stderr)

    try:
        if not installed:
            # Prevent package postinst from exposing the default HTTP site.
            policy = Path("/usr/sbin/policy-rc.d")
            if policy.exists():
                raise RuntimeError("Existing service-start policy requires manual Caddy package installation")
            command(["apt-get", "update"])
            atomic(policy, "#!/bin/sh\nexit 101\n", 0o755)
            fresh_install = True
            try:
                command(["apt-get", "install", "-y", "caddy"], env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
            except Exception:
                rollback_step("package start-policy cleanup", lambda: policy.unlink(missing_ok=True))
                raise
            else:
                policy.unlink()
            # Back up package stock only after confirming its checksum. It is
            # never a rollback target, because its welcome site listens on :80.
            if CADDY_FILE.exists():
                conffiles = command(["dpkg-query", "-W", "-f=${Conffiles}", "caddy"]).stdout.splitlines()
                expected = next((line.split()[1] for line in conffiles
                                 if len(line.split()) >= 2 and line.split()[0] == str(CADDY_FILE)), None)
                checksum = hashlib.md5(CADDY_FILE.read_bytes()).hexdigest()
                if expected is None or checksum != expected:
                    raise RuntimeError("The newly created Caddyfile differs from package stock; preserve it for manual review")
                snapshot(CADDY_FILE)
            managed_configuration = True
            base = "# Claude Bridge site imports.\n"
        else:
            base = original["data"].decode("utf-8") if original is not None else ""
        import_line = "import /etc/caddy/claude-bridge/*.caddy"
        if domain in base:
            raise RuntimeError("This domain already appears in the main Caddyfile; review it manually")
        candidate = base if import_line in base.splitlines() else base.rstrip() + "\n\n" + import_line + "\n"
        atomic(SITE_FILE, site)
        atomic(CADDY_FILE, candidate)
        command(["caddy", "validate", "--config", str(CADDY_FILE), "--adapter", "caddyfile"])
        command(["systemctl", "reload" if was_running else "start", "caddy"])
        command(["systemctl", "enable", "caddy"])
    except Exception:
        if fresh_install:
            rollback_step("new-service stop", lambda: command(["systemctl", "stop", "caddy"]))
            rollback_step("new-service disable", lambda: command(["systemctl", "disable", "caddy"]))
            rollback_step("site restore", lambda: restore(SITE_FILE, original_site))
            if managed_configuration or not CADDY_FILE.exists():
                rollback_step("neutral configuration", lambda: atomic(
                    CADDY_FILE, "# Claude Bridge deployment failed. No HTTP sites are enabled.\n"))
        else:
            rollback_step("configuration restore", lambda: restore(CADDY_FILE, original))
            rollback_step("site restore", lambda: restore(SITE_FILE, original_site))
            if was_enabled == "enabled-runtime":
                rollback_step("persistent enablement removal", lambda: command(["systemctl", "disable", "caddy"]))
                rollback_step("runtime enablement restore", lambda: command(["systemctl", "enable", "--runtime", "caddy"]))
            elif was_enabled in {"enabled", "disabled"}:
                action = "enable" if was_enabled == "enabled" else "disable"
                rollback_step("enablement restore", lambda: command(["systemctl", action, "caddy"]))
            if was_running:
                def restore_running_state():
                    active = subprocess.run(["systemctl", "is-active", "--quiet", "caddy"], capture_output=True).returncode == 0
                    command(["systemctl", "reload" if active else "start", "caddy"])
                rollback_step("running-state restore", restore_running_state)
            else:
                rollback_step("stopped-state restore", lambda: command(["systemctl", "stop", "caddy"]))
        raise
    return {"configured": "https_site", "domain": domain, "upstream": "127.0.0.1:18770"}


def main(configuration):
    if os.geteuid() != 0:
        raise RuntimeError("Root is required for the explicit deployment step")
    if configuration["action"] == "tunnel":
        return tunnel(configuration["public_key"])
    if configuration["action"] == "caddy":
        return caddy(configuration["site"], configuration["domain"])
    raise RuntimeError("Unknown deployment action")
