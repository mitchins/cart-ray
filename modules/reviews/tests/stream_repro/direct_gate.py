"""Require direct Workerd to survive and terminate stalled synthetic uploads."""

import http.client
import socket
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent


def health(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request("GET", "/")
    response = connection.getresponse()
    body = response.read()
    connection.close()
    assert (response.status, body) == (200, b"healthy")


def main():
    """Test both minimal JS and Python handlers on separate direct listeners."""
    with tempfile.TemporaryDirectory(prefix="reviews-direct-stream-") as directory:
        temp = Path(directory)
        for filename in ("worker.mjs", "worker.py"):
            (temp / filename).write_bytes((HERE / filename).read_bytes())
        for config in ("workerd.capnp", "python.capnp"):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            content = (HERE / config).read_text().replace("127.0.0.1:8789", f"127.0.0.1:{port}")
            (temp / config).write_text(content)
            process = subprocess.Popen(
                [str(ROOT / "node_modules/.bin/workerd"), "serve", str(temp / config)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        health(port)
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise
                        time.sleep(0.25)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("POST", "/", body="{")
                response = connection.getresponse()
                assert response.status == 400 and response.read() == b"invalid"
                connection.close()
                with socket.create_connection(("127.0.0.1", port), timeout=5) as stalled:
                    stalled.settimeout(5)
                    stalled.sendall(b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 10\r\n\r\n{")
                    health(port)
                    response = b""
                    deadline = time.monotonic() + 5
                    while b"\r\n" not in response and len(response) < 1024:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("incomplete status line")
                        stalled.settimeout(remaining)
                        chunk = stalled.recv(1024 - len(response))
                        if not chunk:
                            break
                        response += chunk
                    assert b"\r\n" in response and response.startswith(b"HTTP/1.1 400 "), repr(response)
                for _ in range(10):
                    assert process.poll() is None
                    health(port)
                    time.sleep(1)
                print(config, "completed malformed 400; stalled 400; health 10/10", flush=True)
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
