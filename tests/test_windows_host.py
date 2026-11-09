"""Launcher listener ownership regressions. No real processes or network calls."""
from __future__ import annotations

import importlib.util
import ctypes
from pathlib import Path
import socket
import struct
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def host(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "deploy_mobile"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("bridge_windows_host_test", directory / "windows_host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker(pid=24001):
    return SimpleNamespace(pid=pid, poll=Mock(return_value=None), returncode=None)


def health(status=200):
    result = Mock()
    result.__enter__ = Mock(return_value=SimpleNamespace(status=status))
    result.__exit__ = Mock(return_value=False)
    return result


def ipv4_row(address="127.0.0.1", port=18770, pid=24001):
    local = struct.unpack("<I", socket.inet_aton(address))[0]
    return struct.pack("<6I", 1, local, socket.htons(port), 0, 0, pid)


def ipv6_row(address="::1", port=18770, pid=24002):
    return struct.pack("<16sII16sIIII", socket.inet_pton(socket.AF_INET6, address), 0,
                       socket.htons(port), bytes(16), 0, 0, 1, pid)


def table(*rows):
    return struct.pack("<I", len(rows)) + b"".join(rows)


def kernel_api(host, payloads):
    def query(buffer, size, ordered, family, table_class, reserved):
        assert ordered is False and table_class == 3 and reserved == 0
        size = ctypes.cast(size, ctypes.POINTER(host.wintypes.DWORD))
        payload = payloads[family]
        if buffer is None or len(buffer) < len(payload):
            size.contents.value = len(payload)
            return 122
        ctypes.memmove(buffer, payload, len(payload))
        size.contents.value = len(payload)
        return 0
    return Mock(side_effect=query)


def test_kernel_listener_tables_parse_ipv4_and_ipv6_owner_layouts(host):
    payloads = {
        socket.AF_INET: table(ipv4_row(port=80, pid=4), ipv4_row()),
        socket.AF_INET6: table(ipv6_row(), ipv6_row(address="::", pid=24003)),
    }
    query = kernel_api(host, payloads)
    with patch.object(host.ctypes, "WinDLL", return_value=SimpleNamespace(GetExtendedTcpTable=query)), \
         patch.object(host, "powershell", side_effect=AssertionError("Do not fall back to CIM")):
        assert host.port_listeners() == [("127.0.0.1", 24001), ("::1", 24002), ("::", 24003)]
    assert query.call_count == 4
    assert query.argtypes[0] is ctypes.c_void_p and query.restype is host.wintypes.DWORD
    assert {call.args[3] for call in query.call_args_list} == {socket.AF_INET, socket.AF_INET6}


def test_empty_kernel_listener_tables_are_valid(host):
    query = kernel_api(host, {socket.AF_INET: table(), socket.AF_INET6: table()})
    with patch.object(host.ctypes, "WinDLL", return_value=SimpleNamespace(GetExtendedTcpTable=query)):
        assert host.port_listeners() == []


@pytest.mark.parametrize("family,payload", [
    (socket.AF_INET, b""), (socket.AF_INET, b"abc"),
    (socket.AF_INET, struct.pack("<I", 1) + ipv4_row()[:-1]),
    (socket.AF_INET, struct.pack("<I", 2) + ipv4_row()),
    (socket.AF_INET6, struct.pack("<I", 1) + ipv6_row()[:-1]),
    (socket.AF_INET6, struct.pack("<I", 0xffffffff)),
])
def test_truncated_kernel_tables_are_rejected(host, family, payload):
    with pytest.raises(RuntimeError, match="Truncated"):
        host.listener_rows(payload, family)


def test_invalid_target_port_owner_is_rejected(host):
    with pytest.raises(RuntimeError, match="listener owner"):
        host.listener_rows(table(ipv4_row(pid=0)), socket.AF_INET)


def test_kernel_api_error_does_not_claim_port_is_free(host):
    query = Mock(return_value=5)
    with patch.object(host.ctypes, "WinDLL", return_value=SimpleNamespace(GetExtendedTcpTable=query)), \
         patch.object(host, "powershell", side_effect=AssertionError("No ownership fallback")):
        with pytest.raises(OSError):
            host.port_listeners()


def test_ipv6_query_error_rejects_partial_ipv4_success(host):
    read = kernel_api(host, {socket.AF_INET: table(ipv4_row())})
    query = Mock(side_effect=lambda *args: 50 if args[3] == socket.AF_INET6 else read(*args))
    with patch.object(host.ctypes, "WinDLL", return_value=SimpleNamespace(GetExtendedTcpTable=query)):
        with pytest.raises(OSError):
            host.port_listeners()


def test_growing_listener_table_is_retried_with_the_new_size(host):
    payload = table(ipv4_row())
    attempts = 0

    def query(buffer, size, *args):
        nonlocal attempts
        size = ctypes.cast(size, ctypes.POINTER(host.wintypes.DWORD))
        attempts += 1
        if attempts == 1:
            size.contents.value = 4
            return 122
        if attempts == 2:
            size.contents.value = len(payload)
            return 122
        ctypes.memmove(buffer, payload, len(payload))
        size.contents.value = len(payload)
        return 0

    assert host.listener_table(query, socket.AF_INET) == payload
    assert attempts == 3


def test_api_success_with_oversized_result_is_rejected(host):
    attempts = 0

    def query(buffer, size, *args):
        nonlocal attempts
        attempts += 1
        size = ctypes.cast(size, ctypes.POINTER(host.wintypes.DWORD))
        size.contents.value = 4 if attempts == 1 else 5
        return 122 if attempts == 1 else 0

    with pytest.raises(RuntimeError, match="Truncated"):
        host.listener_table(query, socket.AF_INET)


def test_invalid_or_unstable_api_sizes_are_rejected(host):
    def invalid(buffer, size, *args):
        ctypes.cast(size, ctypes.POINTER(host.wintypes.DWORD)).contents.value = 3
        return 122

    with pytest.raises(RuntimeError, match="table size"):
        host.listener_table(invalid, socket.AF_INET)

    def unstable(buffer, size, *args):
        ctypes.cast(size, ctypes.POINTER(host.wintypes.DWORD)).contents.value = 4
        return 122

    with pytest.raises(RuntimeError, match="changed repeatedly"):
        host.listener_table(unstable, socket.AF_INET)


def startup_paths(host, monkeypatch, tmp_path):
    monkeypatch.setattr(host, "RUNTIME", tmp_path)
    monkeypatch.setattr(host.msvcrt, "locking", Mock())
    monkeypatch.setattr(host, "log", Mock())


def test_time_wait_does_not_require_a_temporary_bind(host, monkeypatch, tmp_path):
    startup_paths(host, monkeypatch, tmp_path)

    class ReachedWorkerConfiguration(Exception):
        pass

    # TIME_WAIT is not a listener. The old temporary bind could still fail here.
    with patch.object(host, "port_listeners", return_value=[]), \
         patch.object(host, "ChildJob", side_effect=ReachedWorkerConfiguration), \
         patch("socket.socket", side_effect=PermissionError("Synthetic TIME_WAIT bind failure")) as socket:
        with pytest.raises(ReachedWorkerConfiguration):
            host.run(local_only=True)
        assert not socket.called


def test_preflight_rejects_any_existing_listener(host, monkeypatch, tmp_path):
    startup_paths(host, monkeypatch, tmp_path)
    with patch.object(host, "port_listeners", return_value=[("127.0.0.1", 24001)]), \
         patch.object(host, "vps_paths") as configuration, patch.object(host, "ChildJob") as children:
        with pytest.raises(RuntimeError, match="already has a listener"):
            host.run(local_only=True)
        assert not configuration.called and not children.called


def test_health_200_requires_new_worker_pid_before_and_after_http(host):
    process = worker()
    with patch.object(host, "port_listeners", return_value=[("127.0.0.1", process.pid)]) as listeners, \
         patch.object(host.urllib.request, "urlopen", return_value=health()) as http:
        assert host.ready(process)
        assert listeners.call_count == 2 and http.call_count == 1


@pytest.mark.parametrize("listeners", [
    [("127.0.0.1", 99999)], [("0.0.0.0", 24001)],
    [("127.0.0.1", 24001), ("::", 99999)],
])
def test_unrelated_wildcard_or_multiple_listener_is_never_adopted(host, listeners):
    with patch.object(host, "port_listeners", return_value=listeners), \
         patch.object(host.urllib.request, "urlopen", return_value=health()) as http:
        assert not host.ready(worker())
        assert not http.called


def test_owner_change_during_health_check_is_rejected(host):
    process = worker()
    with patch.object(host, "port_listeners", side_effect=[[("127.0.0.1", process.pid)], [("127.0.0.1", 99999)]]), \
         patch.object(host.urllib.request, "urlopen", return_value=health()):
        assert not host.ready(process)


def test_worker_exit_after_health_response_is_rejected(host):
    process = worker()
    process.poll.side_effect = [None, None, 1]
    with patch.object(host, "port_listeners", return_value=[("127.0.0.1", process.pid)]), \
         patch.object(host.urllib.request, "urlopen", return_value=health()):
        assert not host.ready(process)


def test_waits_for_own_worker_listener_without_claiming_other_health_endpoint(host):
    process = worker()
    with patch.object(host, "port_listeners", side_effect=[[], [("127.0.0.1", process.pid)], [("127.0.0.1", process.pid)]]), \
         patch.object(host.urllib.request, "urlopen", return_value=health()) as http, \
         patch.object(host.time, "sleep") as sleep:
        assert host.ready(process)
        assert sleep.call_count == 1 and http.call_count == 1


def test_ownership_is_rechecked_before_tunnel_reconnect(host, monkeypatch, tmp_path):
    startup_paths(host, monkeypatch, tmp_path)
    key = tmp_path / "synthetic-key"
    known = tmp_path / "synthetic-known-hosts"
    key.touch()
    known.touch()
    monkeypatch.setattr(host, "KEY", key)
    monkeypatch.setattr(host, "KNOWN", known)
    process = worker()
    exited_tunnel = SimpleNamespace(poll=Mock(return_value=255), returncode=255)
    group = SimpleNamespace(spawn=Mock(side_effect=[process, exited_tunnel]), close=Mock())
    with patch.object(host, "port_listeners", return_value=[]), \
         patch.object(host, "vps_paths", return_value=("preview.invalid", None, None)), \
         patch.object(host, "ChildJob", return_value=group), patch.object(host, "ready", return_value=True), \
         patch.object(host, "worker_owns_listener", side_effect=[True, False]) as owns, \
         patch.object(host, "stop") as stop, patch.object(host.time, "sleep"):
        with pytest.raises(RuntimeError, match="no longer owns"):
            host.run()
        assert owns.call_count == 2
        assert group.spawn.call_count == 2, "Do not start another tunnel after losing listener ownership"
        assert group.close.called
        assert stop.call_args_list[0].args == (exited_tunnel,)
        assert stop.call_args_list[1].args == (process,)
