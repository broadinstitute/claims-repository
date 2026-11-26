"""Queries run in the background: a client that may wait longer than the gateway allows (60 s) submits a
GraphQL request or a query template run with ?async=true, gets 202 and a job URL at once, and reads the
result from GET /v1/jobs/{id} when it is done. The work is exactly the synchronous request's (same function,
same response bytes, same claim assertions), only not tied to the HTTP connection.

Jobs live in this process: a result is kept for RESULT_TTL seconds after it finishes and is lost if the task
restarts, and a job is found only by the task that ran it. That holds while the service runs one task
(service.yaml max_tasks: 1); more tasks would need the jobs in the database or the cache bucket.
"""
from __future__ import annotations

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

WORKERS = 4            # jobs running at once; the rest wait their turn
MAX_PENDING = 64       # jobs submitted and not yet finished; past this a submit is refused (429)
MAX_KEPT = 256         # finished jobs kept; the oldest go first
MAX_KEPT_BYTES = 256 * 1024 * 1024   # and their response bodies together, in this task's memory
RESULT_TTL = 3600.0    # seconds a finished job's result is kept

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_pool: ThreadPoolExecutor | None = None


class Busy(RuntimeError):
    """Too many jobs are waiting."""


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="query-job")
    return _pool


def _prune(now: float) -> None:
    done = sorted((j["finished"], i) for i, j in _jobs.items() if j["finished"] is not None)
    excess = len(done) - MAX_KEPT
    held = sum(_size(_jobs[i]["body"]) for _, i in done)
    for k, (finished, i) in enumerate(done):
        if k < excess or now - finished > RESULT_TTL or held > MAX_KEPT_BYTES:
            held -= _size(_jobs[i]["body"])
            del _jobs[i]


def _size(body) -> int:
    return len(body) if isinstance(body, (bytes, str)) else 1024


def submit(kind: str, describe: dict, work) -> dict:
    """Start `work()` -> (http status, response body bytes or JSON-able) in the background; the job's record."""
    now = time.time()
    with _lock:
        _prune(now)
        if sum(1 for j in _jobs.values() if j["finished"] is None) >= MAX_PENDING:
            raise Busy(f"{MAX_PENDING} query jobs are already waiting; try again shortly")
        job_id = secrets.token_urlsafe(18)
        job = _jobs[job_id] = {"id": job_id, "kind": kind, "request": describe, "submitted": now, "started": None,
                               "finished": None, "status": "queued", "http_status": None, "body": None}

    def run():
        job["started"], job["status"] = time.time(), "running"
        try:
            status, body = work()
        except Exception as exc:  # the synchronous route would have answered 500
            status, body = 500, {"detail": f"the query failed: {exc}"}
        job["http_status"], job["body"] = status, body
        job["status"] = "done" if status == 200 else "failed"
        job["finished"] = time.time()

    _executor().submit(run)
    return job


def get(job_id: str) -> dict | None:
    with _lock:
        _prune(time.time())
        return _jobs.get(job_id)


def summary(job: dict, url: str) -> dict:
    """What a client sees of a job, without its result."""
    def iso(t):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None

    end = job["finished"] or time.time()
    return {"id": job["id"], "kind": job["kind"], "status": job["status"], "url": url, "result_url": f"{url}/result",
            "request": job["request"],
            "submitted_at": iso(job["submitted"]), "started_at": iso(job["started"]),
            "finished_at": iso(job["finished"]),
            "seconds": round(end - (job["started"] or end), 3),
            "expires_at": iso(job["finished"] + RESULT_TTL) if job["finished"] else None,
            "http_status": job["http_status"]}
