"""Operator-configured pinned VPS transport. Never changes network routes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import threading

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import core


def deployment_data_dir():
    default = SOURCE.parent / "data" if SOURCE.name == "mobile-service" else SOURCE / "data"
    return Path(os.environ.get("CLAUDE_BRIDGE_DATA", str(default))).expanduser().resolve()


def deployment_settings():
    return core.mobile_settings(deployment_data_dir())


def vps_paths():
    settings = deployment_settings()
    host = settings.get("vps_host", "")
    if (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host)
            or host == "example.com" or host.endswith(".example.com")):
        raise RuntimeError("Configure your own vps_host in data/mobile/settings.json first")
    paths = []
    for field, default in (("admin_key", "~/.ssh/id_ed25519"), ("known_hosts", "~/.ssh/known_hosts")):
        value = settings.get(field, default)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Invalid local deployment path: " + field)
        paths.append(Path(value).expanduser().resolve())
    return host, *paths


def vps_port():
    value = deployment_settings().get("vps_port", 22)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise RuntimeError("vps_port must be an integer between 1 and 65535")
    return value


def physical_route():
    # Proxy/TUN adapters may advertise more-specific routes. Select an up physical
    # adapter with a real IPv4 gateway, then bind both its index and source address.
    command = r"""
$ErrorActionPreference = 'Stop'
$choices = foreach ($adapter in Get-NetAdapter -Physical | Where-Object Status -eq 'Up') {
    $configuration = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex
    if (-not $configuration.IPv4DefaultGateway) { continue }
    $route = Get-NetRoute -AddressFamily IPv4 -InterfaceIndex $adapter.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric | Select-Object -First 1
    $address = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $adapter.ifIndex |
        Where-Object { $_.AddressState -eq 'Preferred' -and -not $_.SkipAsSource -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -First 1
    if ($route -and $address) {
        $interface = Get-NetIPInterface -AddressFamily IPv4 -InterfaceIndex $adapter.ifIndex
        [pscustomobject]@{ index = [int]$adapter.ifIndex; source = $address.IPAddress; metric = [int]$route.RouteMetric + [int]$interface.InterfaceMetric }
    }
}
$selected = $choices | Sort-Object metric,index | Select-Object -First 1
if (-not $selected) { throw 'No up physical IPv4 default route' }
$selected | ConvertTo-Json -Compress
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Cannot detect an active physical IPv4 default route")
    route = json.loads(result.stdout)
    socket.inet_aton(route["source"])
    if int(route["index"]) < 1:
        raise RuntimeError("Invalid physical interface index")
    return route


def direct_socket(host, port=None):
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(15)
    if os.name == "nt" and deployment_settings().get("bind_physical_interface", False) is True:
        route = physical_route()
        connection.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", int(route["index"])))
        connection.bind((route["source"], 0))
    connection.connect((host, port if port is not None else vps_port()))
    return connection


def proxy():
    host, _, _ = vps_paths()
    connection = direct_socket(host)
    connection.settimeout(None)

    def upload():
        try:
            while block := sys.stdin.buffer.read1(65536):
                connection.sendall(block)
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=upload, daemon=True).start()
    try:
        while block := connection.recv(65536):
            sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        proxy()
    except Exception:
        # This process is SSH's binary transport. Do not put exception details or
        # deployment paths onto its stdout/stderr streams.
        raise SystemExit(1)
