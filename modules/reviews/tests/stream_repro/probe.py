"""Report raw transport outcomes while an upload is intentionally stalled."""

import http.client
import socket
import sys
import time

port = int(sys.argv[1])


def health():
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("GET", "/")
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data.decode()


def report_health(label):
    """Keep recording transport failures, including during the upload."""
    try:
        print(label, health(), flush=True)
    except OSError as exc:
        print(label, "transport failure:", type(exc).__name__, flush=True)


conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
conn.request("POST", "/", body="{", headers={"Content-Type": "application/json"})
response = conn.getresponse()
print("completed:", response.status, response.read().decode(), flush=True)
conn.close()
with socket.create_connection(("127.0.0.1", port), timeout=3) as stalled:
    stalled.settimeout(3)
    stalled.sendall(b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 10\r\n\r\n{")
    start = time.monotonic()
    time.sleep(0.25)
    report_health("during upload:")
    time.sleep(1.25)
    report_health("after handler deadline:")
    try:
        result = stalled.recv(1024)
        print("stalled:", repr(result), "after", round(time.monotonic() - start, 2), flush=True)
    except (TimeoutError, ConnectionResetError) as exc:
        print("stalled:", type(exc).__name__, "after", round(time.monotonic() - start, 2), flush=True)
for _ in range(10):
    report_health("after:")
    time.sleep(1)
