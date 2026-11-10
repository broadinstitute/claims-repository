"""Process-local API telemetry for the admin console.

Bounded, in-memory counters: per route template (method + path pattern) the
request count, 4xx/5xx counts, mean latency over the process lifetime, and
p50/p95 over the latest SAMPLES requests; per GraphQL request the outcome, the
root fields asked for, and how many claims it minted vs re-asserted. Counters
reset when the process restarts and cover one process only (QA and prod run a
single task). Durable claim telemetry (mints, assertion counts, timestamps)
comes from the database, not from here.
"""
from __future__ import annotations

import threading
import time
from collections import Counter, deque

SAMPLES = 500
STARTED_AT = time.time()
_lock = threading.Lock()
_routes: dict[tuple[str, str], dict] = {}
_graphql = {"requests": 0, "ok": 0, "failed": 0, "minted": 0, "reasserted": 0, "last_at": None}
_root_fields: Counter = Counter()


def record_request(method: str, route: str, status: int, ms: float) -> None:
    with _lock:
        r = _routes.get((method, route))
        if r is None:
            r = _routes[(method, route)] = {
                "count": 0, "errors_4xx": 0, "errors_5xx": 0, "total_ms": 0.0,
                "samples": deque(maxlen=SAMPLES), "last_at": None, "last_status": None,
            }
        r["count"] += 1
        r["errors_4xx"] += 400 <= status < 500
        r["errors_5xx"] += status >= 500
        r["total_ms"] += ms
        r["samples"].append(ms)
        r["last_at"] = time.time()
        r["last_status"] = status


def record_graphql(success: bool, minted: int, reasserted: int, root_fields) -> None:
    with _lock:
        _graphql["requests"] += 1
        _graphql["ok" if success else "failed"] += 1
        if success:
            _graphql["minted"] += minted
            _graphql["reasserted"] += reasserted
        _graphql["last_at"] = time.time()
        _root_fields.update(root_fields)


def _pct(sorted_values, q):
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def _iso(ts):
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def snapshot() -> dict:
    with _lock:
        routes = []
        for (method, route), r in _routes.items():
            samples = sorted(r["samples"])
            routes.append({
                "method": method, "route": route, "count": r["count"],
                "errors4xx": r["errors_4xx"], "errors5xx": r["errors_5xx"],
                "meanMs": round(r["total_ms"] / r["count"], 1) if r["count"] else None,
                "p50Ms": _pct(samples, 0.50) and round(_pct(samples, 0.50), 1),
                "p95Ms": _pct(samples, 0.95) and round(_pct(samples, 0.95), 1),
                "samples": len(samples), "lastAt": _iso(r["last_at"]), "lastStatus": r["last_status"],
            })
        graphql = dict(_graphql, last_at=_iso(_graphql["last_at"]))
        fields = [{"field": f, "count": c} for f, c in _root_fields.most_common(25)]
    routes.sort(key=lambda r: -r["count"])
    return {
        "startedAt": _iso(STARTED_AT), "uptimeSeconds": int(time.time() - STARTED_AT),
        "sampleWindow": SAMPLES, "routes": routes,
        "graphql": {"requests": graphql["requests"], "ok": graphql["ok"], "failed": graphql["failed"],
                    "minted": graphql["minted"], "reasserted": graphql["reasserted"],
                    "lastAt": graphql["last_at"], "rootFields": fields},
    }


def reset() -> None:
    """Tests only."""
    with _lock:
        _routes.clear()
        _root_fields.clear()
        _graphql.update({"requests": 0, "ok": 0, "failed": 0, "minted": 0, "reasserted": 0, "last_at": None})


class RequestTelemetry:
    """Pure ASGI middleware: times each HTTP request and records it under its
    matched route template (/v1/objects/{object_id}, not the raw path, so
    cardinality stays bounded). Admin console traffic is not recorded."""

    def __init__(self, app, skip_prefixes=("/v1/admin",)):
        self.app = app
        self.skip = skip_prefixes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Mounted under a platform prefix, `path` still carries root_path.
        path, root = scope.get("path", ""), scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root):]
        if path.startswith(self.skip):
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or "(unmatched)"
            record_request(scope.get("method", "?"), template, status["code"],
                           (time.perf_counter() - started) * 1000)
