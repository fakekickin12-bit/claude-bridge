"""Review or explicitly apply one VPS change, using the existing pinned root SSH key."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys

from transport import deployment_data_dir, deployment_settings, direct_socket, vps_paths, vps_port
from device_auth import pairing_url

ROOT = Path(__file__).resolve().parent
PUBLIC_KEY = deployment_data_dir() / "mobile/host/tunnel_ed25519.pub"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--apply-tunnel", action="store_true", help="Create only the restricted user and SSH loopback-forward policy")
    actions.add_argument("--apply-caddy", action="store_true", help="Install/activate the domain HTTPS site on TCP 80/443")
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--data-dir", type=Path, help="Use the same data directory as desktop Bridge")
    parser.add_argument("--config", type=Path, help="Path to your private mobile settings.json")
    args = parser.parse_args()
    if args.data_dir is not None:
        os.environ["CLAUDE_BRIDGE_DATA"] = str(args.data_dir.expanduser().resolve())
    if args.config is not None:
        os.environ["CLAUDE_BRIDGE_MOBILE_CONFIG"] = str(args.config.expanduser().resolve())
    if not args.apply_tunnel and not args.apply_caddy:
        print("Plan only. No connection or changes performed.")
        print("1. windows_host.py prepare creates a dedicated local tunnel key.")
        print("2. --apply-tunnel provisions SSH remote-only 127.0.0.1:18770; no public web listener.")
        print("3. windows_host.py run starts the local worker and restricted reverse tunnel.")
        print("4. After configuring your domain/DNS, --apply-caddy activates your HTTPS origin on 80/443.")
        print("Other server services and firewall rules are outside these actions.")
        return
    import paramiko
    host, root_key, known = vps_paths()
    if not root_key.is_file() or not known.is_file():
        raise RuntimeError("The existing root SSH key or pinned known_hosts file is missing")
    from urllib.parse import urlsplit
    origin = pairing_url(deployment_data_dir())
    domain = urlsplit(origin).netloc
    if ":" in domain:
        raise RuntimeError("The automatic Caddy deployer requires a standard HTTPS domain on port 443")
    site = (ROOT / "Caddyfile.bridge").read_text(encoding="utf-8").replace("bridge.example.com", domain)
    public_key = args.public_key or deployment_data_dir() / "mobile/host/tunnel_ed25519.pub"
    configuration = ({"action": "tunnel", "public_key": public_key.read_text(encoding="utf-8").strip()}
                     if args.apply_tunnel else {"action": "caddy", "site": site, "domain": domain})
    client = paramiko.SSHClient()
    client.load_host_keys(str(known))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    connection = direct_socket(host)
    try:
        client.connect(host, port=vps_port(), username=deployment_settings().get("admin_user", "root"),
                       key_filename=str(root_key), sock=connection,
                       look_for_keys=False, allow_agent=False, timeout=15, auth_timeout=15, banner_timeout=15)
        source = (ROOT / "remote_setup.py").read_text(encoding="utf-8")
        script = "import json,sys; request=json.load(sys.stdin); scope={}; exec(compile(request['source'],'bridge-deploy','exec'),scope); print(json.dumps(scope['main'](request['configuration'])))"
        stdin, stdout, stderr = client.exec_command("python3 -c " + shlex.quote(script), timeout=600)
        stdin.write(json.dumps({"source": source, "configuration": configuration}))
        stdin.flush()
        stdin.channel.shutdown_write()
        output, errors = stdout.read().decode(), stderr.read().decode()
        result = stdout.channel.recv_exit_status()
        if result:
            # Remote errors report fixed operations and exception types. Avoid
            # dumping process output that could contain unrelated configuration.
            print("VPS deployment failed. Review the server state before retrying.", file=sys.stderr)
            print(errors.splitlines()[-1][:400] if errors else "remote process exited nonzero", file=sys.stderr)
            raise SystemExit(result)
        print(output.strip())
    finally:
        client.close()
        connection.close()


if __name__ == "__main__":
    main()
