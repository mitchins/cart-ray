from __future__ import annotations

from kinglet import Kinglet

app = Kinglet()


@app.get("/health")
async def health(_request):
    return {"service": "cartray", "mode": "test-only", "status": "ok"}


try:
    from workers import WorkerEntrypoint
except ImportError:

    class WorkerEntrypoint:  # pragma: no cover - local test shim
        pass


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await app(request, self.env)
