"""Current-user worker and restricted reverse tunnel. Run with --help first."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import msvcrt
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import urllib.request

from transport import deployment_data_dir, deployment_settings, vps_paths, vps_port
from device_auth import pairing_url

SOURCE = Path(__file__).resolve().parents[1]
DATA = deployment_data_dir()
RUNTIME = DATA / "mobile" / "host"
KEY = RUNTIME / "tunnel_ed25519"
KNOWN = RUNTIME / "known_hosts"
ORIGIN = os.environ.get("CLAUDE_BRIDGE_ORIGIN", deployment_settings().get("public_origin", ""))
PORT = 18770
USER = "claude-bridge-tunnel"
TASK = "Claude Bridge Mobile"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
PYTHON = Path(sys.executable).with_name("python.exe")


def powershell(script, *arguments):
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script, *arguments],
                          check=True, capture_output=True, text=True, creationflags=NO_WINDOW)


def ensure_user():
    global ORIGIN
    if os.name != "nt":
        raise RuntimeError("Run this launcher from the Windows desktop account that configured Bridge")
    if not (DATA / "config.json").is_file():
        raise RuntimeError("The existing Bridge data/config.json was not found")
    ORIGIN = pairing_url(DATA)


def prepare():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    account = subprocess.check_output(["whoami"], text=True, creationflags=NO_WINDOW).strip()
    subprocess.run(["icacls", str(RUNTIME), "/inheritance:r", "/grant:r", account + ":(OI)(CI)F", "/Q"],
                   capture_output=True, check=True, creationflags=NO_WINDOW)
    if not KEY.exists():
        subprocess.run(["ssh-keygen.exe", "-q", "-t", "ed25519", "-N", "", "-C", "claude-bridge-tunnel",
                        "-f", str(KEY)], check=True, capture_output=True, creationflags=NO_WINDOW)
    if not KEY.with_suffix(".pub").is_file():
        raise RuntimeError("The tunnel public key is missing; do not replace the existing private key")
    subprocess.run(["icacls", str(KEY), "/inheritance:r", "/grant:r", account + ":F", "/Q"],
                   check=True, capture_output=True, creationflags=NO_WINDOW)
    host, _, trusted_known = vps_paths()
    host_pin = host if vps_port() == 22 else f"[{host}]:{vps_port()}"
    result = subprocess.run(["ssh-keygen.exe", "-F", host_pin, "-f", str(trusted_known)],
                            check=True, capture_output=True, text=True, creationflags=NO_WINDOW)
    pins = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    if not pins:
        raise RuntimeError("The VPS does not have an existing pinned SSH host key")
    KNOWN.write_text("\n".join(pins) + "\n", encoding="utf-8")
    print("Prepared the dedicated tunnel key and pinned host key locally")


def log(message):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    destination = RUNTIME / "host.log"
    if destination.exists() and destination.stat().st_size > 1024 * 1024:
        os.replace(destination, RUNTIME / "host.previous.log")
    with destination.open("a", encoding="utf-8") as stream:
        stream.write(datetime.now(timezone.utc).isoformat() + " " + message + "\n")


class ChildJob:
    """Windows closes the worker and SSH descendants if the launcher is killed."""

    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Counters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def spawn(self, arguments):
        process = subprocess.Popen(arguments, cwd=SOURCE, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(process._handle)):
            process.terminate()
            raise ctypes.WinError(ctypes.get_last_error())
        return process

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def listener_table(query, family):
    size = wintypes.DWORD()
    result = query(None, ctypes.byref(size), False, family, 3, 0)
    if result not in (0, 122):  # ERROR_INSUFFICIENT_BUFFER
        raise ctypes.WinError(result)
    for _ in range(3):
        if not 4 <= size.value <= 16 * 1024 * 1024:
            raise RuntimeError("Invalid TCP listener table size")
        buffer = ctypes.create_string_buffer(size.value)
        result = query(buffer, ctypes.byref(size), False, family, 3, 0)
        if result == 122:
            continue
        if result:
            raise ctypes.WinError(result)
        if not 4 <= size.value <= len(buffer):
            raise RuntimeError("Truncated TCP listener table")
        return buffer.raw[:size.value]
    raise RuntimeError("TCP listener table changed repeatedly during the ownership check")


def listener_rows(data, family):
    row_size = 24 if family == socket.AF_INET else 56
    if len(data) < 4:
        raise RuntimeError("Truncated TCP listener table header")
    count = struct.unpack_from("<I", data)[0]
    if 4 + count * row_size > len(data):
        raise RuntimeError("Truncated TCP listener table rows")
    listeners = []
    for index in range(count):
        offset = 4 + index * row_size
        if family == socket.AF_INET:
            _, address, port, _, _, pid = struct.unpack_from("<6I", data, offset)
            address = socket.inet_ntoa(struct.pack("<I", address))
        else:
            address, _, port, _, _, _, _, pid = struct.unpack_from("<16sII16sIIII", data, offset)
            address = socket.inet_ntop(socket.AF_INET6, address)
        if socket.ntohs(port & 0xffff) != PORT:
            continue
        if not pid:
            raise RuntimeError("Invalid TCP listener owner")
        listeners.append((address, pid))
    return listeners


def port_listeners():
    query = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                      wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    query.restype = wintypes.DWORD
    # Table class 3 (TCP_TABLE_OWNER_PID_LISTENER) filters listeners in the kernel.
    # Do not use CIM's State projection, which can be empty on this Windows build.
    return [row for family in (socket.AF_INET, socket.AF_INET6)
            for row in listener_rows(listener_table(query, family), family)]


def worker_owns_listener(process):
    return (process.poll() is None and port_listeners() == [("127.0.0.1", process.pid)]
            and process.poll() is None)


def ready(process):
    for _ in range(40):
        if process.poll() is not None:
            return False
        listeners = port_listeners()
        if listeners and listeners != [("127.0.0.1", process.pid)]:
            return False
        if listeners:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=1) as response:
                    if response.status == 200:
                        return worker_owns_listener(process)
            except OSError:
                pass
        time.sleep(.25)
    return False


def run(local_only=False):
    if not local_only and (not KEY.is_file() or not KNOWN.is_file()):
        raise RuntimeError("Run the prepare command and provision the restricted VPS tunnel user first")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "launcher.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            log("launcher already running")
            return
        # Inspect listeners without reserving a port that may still have TIME_WAIT connections.
        # An existing listener belongs to an unrelated or orphan worker and is never adopted.
        if port_listeners():
            raise RuntimeError("The local worker port already has a listener; refusing to attach")
        ssh_arguments = None
        if not local_only:
            host, _, _ = vps_paths()
            proxy = subprocess.list2cmdline([str(PYTHON), str(Path(__file__).with_name("transport.py"))])
            ssh_arguments = ["ssh.exe", "-F", "none", "-N", "-T", "-p", str(vps_port()),
                             "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                             "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={KNOWN}",
                             "-o", "GlobalKnownHostsFile=NUL", "-o", f"ProxyCommand={proxy}",
                             "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=20",
                             "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=15", "-o", "ForwardAgent=no",
                             "-i", str(KEY), "-R", f"127.0.0.1:{PORT}:127.0.0.1:{PORT}", f"{USER}@{host}"]
        group = ChildJob()
        worker = tunnel = None
        delay = 2
        try:
            log("launcher started; local-only" if local_only else "launcher started")
            worker = group.spawn([str(PYTHON), str(SOURCE / "mobile_service.py"), "--data-dir", str(DATA),
                                  "--origin", ORIGIN, "--port", str(PORT)])
            if not ready(worker):
                raise RuntimeError("The local worker failed its startup health check")
            log("local worker ready")
            while True:
                if worker.poll() is not None:
                    log(f"local worker exited code={worker.returncode}; tunnel stopping")
                    raise RuntimeError("Local worker stopped")
                if not local_only and (tunnel is None or tunnel.poll() is not None):
                    if tunnel is not None:
                        log(f"tunnel exited code={tunnel.returncode}; reconnect scheduled")
                        time.sleep(delay)
                        delay = min(delay * 2, 60)
                    if not worker_owns_listener(worker):
                        raise RuntimeError("The local worker no longer owns its loopback listener")
                    tunnel = group.spawn(ssh_arguments)
                    log("tunnel connection started")
                time.sleep(1)
        except KeyboardInterrupt:
            log("launcher stopped by user")
        finally:
            stop(tunnel)
            stop(worker)
            group.close()


def install_task():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("The current Python installation does not include pythonw.exe")
    launch = [str(Path(__file__).resolve()), "run", "--data-dir", str(DATA), "--origin", ORIGIN]
    if os.environ.get("CLAUDE_BRIDGE_MOBILE_CONFIG"):
        launch += ["--config", str(Path(os.environ["CLAUDE_BRIDGE_MOBILE_CONFIG"]).expanduser().resolve())]
    arguments = subprocess.list2cmdline(launch)
    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = f"""
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute {literal(pythonw)} -Argument {literal(arguments)} -WorkingDirectory {literal(SOURCE)}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName {literal(TASK)} -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
"""
    powershell(script)
    print("Registered an interactive current-user logon task; it has not been started")


def main():
    global DATA, RUNTIME, KEY, KNOWN, ORIGIN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "install-task", "start-task", "stop-task", "remove-task"))
    parser.add_argument("--local-only", action="store_true", help="For run, start only the Windows loopback worker")
    parser.add_argument("--data-dir", type=Path, help="Use the same data directory as your desktop Bridge")
    parser.add_argument("--config", type=Path, help="Path to your private mobile settings.json")
    parser.add_argument("--origin", help="Override the configured public HTTPS origin")
    args = parser.parse_args()
    if args.data_dir is not None:
        os.environ["CLAUDE_BRIDGE_DATA"] = str(args.data_dir.expanduser().resolve())
    if args.config is not None:
        os.environ["CLAUDE_BRIDGE_MOBILE_CONFIG"] = str(args.config.expanduser().resolve())
    if args.origin is not None:
        os.environ["CLAUDE_BRIDGE_ORIGIN"] = args.origin
    DATA = deployment_data_dir()
    RUNTIME = DATA / "mobile/host"
    KEY, KNOWN = RUNTIME / "tunnel_ed25519", RUNTIME / "known_hosts"
    ORIGIN = os.environ.get("CLAUDE_BRIDGE_ORIGIN", deployment_settings().get("public_origin", ""))
    ensure_user()
    if args.command == "prepare":
        prepare()
    elif args.command == "run":
        run(args.local_only)
    elif args.command == "install-task":
        install_task()
    else:
        verb = {"start-task": "Start", "stop-task": "Stop", "remove-task": "Unregister"}[args.command]
        suffix = " -Confirm:$false" if args.command == "remove-task" else ""
        powershell(f"{verb}-ScheduledTask -TaskName '{TASK}'{suffix}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log("launcher failed type=" + type(error).__name__)
        if sys.stderr is not None:
            print(str(error), file=sys.stderr)
        raise SystemExit(1)
