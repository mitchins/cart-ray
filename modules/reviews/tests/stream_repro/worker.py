"""Minimal Python stream timeout without D1 or Reviews logic."""

from js import Promise, clearTimeout, setTimeout
from pyodide.ffi import create_proxy, to_js
from workers import Response, WorkerEntrypoint


async def read_with_timeout(reader):
    state = {}

    def executor(resolve, _reject):
        state["callback"] = create_proxy(lambda: resolve("timeout"))
        state["timer"] = setTimeout(state["callback"], 1000)

    proxy = create_proxy(executor)
    try:
        timer = Promise.new(proxy)
    finally:
        proxy.destroy()
    try:
        return await Promise.race(to_js([reader.read(), timer]))
    finally:
        clearTimeout(state["timer"])
        state["callback"].destroy()


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if request.method == "GET":
            return Response("healthy")
        reader = request.body.getReader()
        try:
            while True:
                result = await read_with_timeout(reader)
                if result == "timeout":
                    await reader.cancel()
                    print("handler returned 400 after timeout")
                    return Response("incomplete", status=400)
                if result.done:
                    print("handler returned 400 for completed body")
                    return Response("invalid", status=400)
        finally:
            reader.releaseLock()
