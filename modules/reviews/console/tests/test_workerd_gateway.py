"""Safety checks for the disposable, loopback-only browser qualification fixture."""

import importlib.util
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).with_name("workerd_gateway.py")
spec = importlib.util.spec_from_file_location("console_workerd_gateway", PATH)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def test_partial_and_slow_request_bodies_end_by_absolute_deadline():
    left, right = socket.socketpair()
    handler = gateway.Gateway.__new__(gateway.Gateway)
    handler.connection = left
    handler.rfile = left.makefile("rb")
    handler.close_connection = False
    done = threading.Event()

    def trickle():
        while not done.is_set():
            try:
                right.sendall(b"x")
            except OSError:
                return
            time.sleep(0.03)

    sender = threading.Thread(target=trickle, daemon=True)
    sender.start()
    try:
        started = time.monotonic()
        assert handler.read_bounded_body(100, timeout=0.15) is None
        assert time.monotonic() - started < 0.8
        assert handler.close_connection is True
    finally:
        done.set()
        sender.join(timeout=1)
        handler.rfile.close()
        left.close()
        right.close()


def test_complete_body_is_read_without_losing_bytes():
    left, right = socket.socketpair()
    handler = gateway.Gateway.__new__(gateway.Gateway)
    handler.connection = left
    handler.rfile = left.makefile("rb")
    handler.close_connection = False
    try:
        right.sendall(b'{"revision_id":"synthetic"}')
        assert handler.read_bounded_body(len(b'{"revision_id":"synthetic"}'), timeout=1) == (
            b'{"revision_id":"synthetic"}'
        )
        assert not handler.close_connection
    finally:
        handler.rfile.close()
        left.close()
        right.close()


def test_stalled_http_headers_do_not_block_fixture_shutdown():
    server = gateway.FixtureServer(("127.0.0.1", 0), gateway.Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(server.server_address, timeout=2)
    client.sendall(b"POST /unfinished HTTP/1.1\r\nHost: ")
    try:
        started = time.monotonic()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert time.monotonic() - started < 1.5
    finally:
        client.close()


def test_stalled_http_body_returns_408_without_forwarding(monkeypatch):
    server = gateway.FixtureServer(("127.0.0.1", 0), gateway.Gateway)
    port = server.server_address[1]
    monkeypatch.setattr(gateway, "PORT", port)
    monkeypatch.setattr(gateway, "ORIGIN", f"http://127.0.0.1:{port}")
    # A stalled body must fail before worker ownership checks or JWT creation.
    monkeypatch.setattr(gateway, "require_owned", lambda *_: pytest.fail("stalled body was forwarded"))
    server.worker_process = object()
    server.marker_subject = "unused"
    server.marker_version = 1
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(server.server_address, timeout=4)
    client.settimeout(4)
    client.sendall(
        f"POST /reviews-admin/v1/contributions/{'a' * 64}/approve HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n"
        "Content-Type: application/json\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{".encode()
    )
    try:
        started = time.monotonic()
        assert b" 408 " in client.recv(4096)
        assert time.monotonic() - started < 3.5
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_readiness_requires_own_listener_and_unpredictable_database_marker(monkeypatch):
    process = SimpleNamespace(pid=1234, poll=lambda: None)
    monkeypatch.setattr(gateway.sys, "platform", "linux")
    requests = []
    monkeypatch.setattr(gateway.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="4321\n"))
    monkeypatch.setattr(gateway.os, "getpgid", lambda _pid: 9999)
    monkeypatch.setattr(gateway.urllib.request, "urlopen", lambda *a, **k: requests.append(a))
    assert not gateway.owns_marker(process, "fixture-example", 314)
    assert requests == [], "foreign listener must not receive a readiness request or any credential"

    monkeypatch.setattr(gateway.os, "getpgid", lambda _pid: process.pid)
    monkeypatch.setattr(
        gateway.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(json.dumps({"subject_id": "fixture-example", "snapshot_id": "native-314"}).encode()),
    )
    assert gateway.owns_marker(process, "fixture-example", 314)
    assert not gateway.owns_marker(process, "fixture-different", 314)
    assert not gateway.owns_marker(process, "fixture-example", 315)


def test_interrupted_worker_start_reaps_owned_process(monkeypatch):
    process = SimpleNamespace(pid=1234, poll=lambda: None)
    stopped = []
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(gateway, "owns_marker", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(gateway, "stop_owned", stopped.append)
    with pytest.raises(KeyboardInterrupt):
        gateway.start_owned("/tmp/disposable", "fixture-example", 314)
    assert stopped == [process]


def test_interrupted_installer_reaps_owned_process(monkeypatch):
    process = SimpleNamespace(pid=1234, wait=lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    stopped = []
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(gateway, "stop_owned", stopped.append)
    with pytest.raises(KeyboardInterrupt):
        gateway.run_owned(["synthetic"], env={})
    assert stopped == [process]


def test_sigterm_cleans_fixture_directory_even_during_setup(tmp_path):
    directory = tmp_path / "disposable"
    directory.mkdir()
    program = """
import importlib.util, signal, sys, time
p = importlib.util.spec_from_file_location('test_fixture', sys.argv[1])
m = importlib.util.module_from_spec(p); p.loader.exec_module(m)
m.tempfile.mkdtemp = lambda **kwargs: sys.argv[2]
def wait(*args, **kwargs):
    print('ready', flush=True)
    time.sleep(30)
m.run_owned = wait
signal.signal(signal.SIGTERM, m.terminate)
try: m.main([])
except KeyboardInterrupt: pass
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(PATH), str(directory)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        assert not directory.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_sigterm_closes_gateway_stops_worker_and_removes_d1(tmp_path):
    directory, receipt = tmp_path / "disposable", tmp_path / "cleanup.txt"
    directory.mkdir()
    program = """
import importlib.util, signal, sys, time
p = importlib.util.spec_from_file_location('test_fixture', sys.argv[1])
m = importlib.util.module_from_spec(p); p.loader.exec_module(m)
m.tempfile.mkdtemp = lambda **kwargs: sys.argv[2]
m.run_owned = lambda *args, **kwargs: None
m.start_owned = lambda *args, **kwargs: object()
m.require_owned = lambda *args: None
def record(value):
    with open(sys.argv[3], 'a') as f: f.write(value + '\\n')
m.stop_owned = lambda process: record('worker-stopped')
def call(path, *args, **kwargs):
    if path.endswith('/subjects'): return 201, {}
    if path.endswith('/test-capabilities'): return 201, {'test_capability':'synthetic'}
    return 202, {'review_id':'a' * 64}
m.call = call
class FakeServer:
    def __init__(self, *args): pass
    def serve_forever(self): print('ready', flush=True); time.sleep(30)
    def server_close(self): record('server-closed')
m.FixtureServer = FakeServer
signal.signal(signal.SIGTERM, m.terminate)
m.main([])
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(PATH), str(directory), str(receipt)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert process.stdout.readline().strip().endswith("ready at http://127.0.0.1:48974/")
        assert process.stdout.readline().strip() == "ready"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        assert receipt.read_text().splitlines() == ["worker-stopped", "server-closed", "worker-stopped"]
        assert not directory.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_all_console_modules_are_served_by_loopback_gateway(monkeypatch):
    import urllib.request

    server = gateway.FixtureServer(("127.0.0.1", 0), gateway.Gateway)
    port = server.server_address[1]
    monkeypatch.setattr(gateway, "PORT", port)
    monkeypatch.setattr(gateway, "ORIGIN", f"http://127.0.0.1:{port}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for asset in ("reply-controls.mjs", "delivery-controls.mjs"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/{asset}", timeout=2) as response:
                assert response.status == 200
                assert response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_macos_listener_ownership_fails_closed_for_foreign_and_malformed_pids(monkeypatch):
    monkeypatch.setattr(gateway.sys, "platform", "darwin")
    monkeypatch.setattr(gateway, "WORKER_PORT", 12345)
    process = SimpleNamespace(pid=1234, poll=lambda: None)
    monkeypatch.setattr(gateway.os, "getpgid", lambda pid: 1234 if pid == 42 else 9999)
    header = "Proto Recv-Q Send-Q Local Foreign state rx tx rh sh process:pid\n"
    row = "tcp4 0 0 127.0.0.1.12345 *.* LISTEN 0 0 0 0 worker:42\n"
    foreign = "tcp6 0 0 ::1.12345 *.* LISTEN 0 0 0 0 other:43\n"
    unrelated = "tcp4 0 0 127.0.0.1.123456 *.* LISTEN 0 0 0 0 other:43\n"
    output = [header + row + unrelated]
    monkeypatch.setattr(gateway.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=output[0]))
    assert gateway.owns_listener(process)
    output[0] += foreign
    assert not gateway.owns_listener(process)
    output[0] = header + row.replace("worker:42", "worker:?")
    assert not gateway.owns_listener(process)
    output[0] = row
    with pytest.raises(RuntimeError):
        gateway.owns_listener(process)
