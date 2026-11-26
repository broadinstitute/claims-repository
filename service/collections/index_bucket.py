"""Where a release's Parquet index lives when it is not in this service: in a bucket the data's owner keeps
(docs/publishing-to-a-deployment.md). The service database records, per dataset, the index's location and
each file's sha256, size and S3 version; the service reads the files into a local cache, checks them against
that record, and serves from the cache. Nothing of the index is kept in the database.

Locations are `s3://bucket/prefix` (the bucket must be one this deployment may read: CLAIMS_INDEX_BUCKETS)
or, for tests and local runs only, `file:///absolute/path` when CLAIMS_INDEX_ALLOW_FILE=1.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

FILES = ("rows.parquet", "metrics.parquet", "edges.parquet", "counts.json")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_PART_RE = re.compile(r"[A-Za-z0-9._=-]+\Z")


class IndexError_(ValueError):
    """An index location or file that cannot be used."""


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def allowed_buckets() -> set[str]:
    return {b.strip() for b in os.environ.get("CLAIMS_INDEX_BUCKETS", "").split(",") if b.strip()}


def parse(location: str, *, server: bool = True) -> tuple[str, str, str]:
    """(scheme, bucket or directory, prefix) of an index location, refused unless strictly well formed (and,
    on the server, in a bucket this deployment may read)."""
    if not isinstance(location, str) or len(location) > 512:
        raise IndexError_("an index location is s3://bucket/prefix")
    if location.startswith("file:///") and os.environ.get("CLAIMS_INDEX_ALLOW_FILE") == "1":
        path = Path(location[len("file://"):])
        if ".." in path.parts or not path.is_absolute():
            raise IndexError_(f"{location}: not an absolute path")
        return "file", str(path), ""
    if not location.startswith("s3://"):
        raise IndexError_(f"{location}: an index location is s3://bucket/prefix")
    bucket, _, prefix = location[len("s3://"):].rstrip("/").partition("/")
    parts = prefix.split("/") if prefix else []
    if not _BUCKET_RE.match(bucket) or ".." in bucket or not all(_PART_RE.match(p) and p not in (".", "..")
                                                                  for p in parts):
        raise IndexError_(f"{location}: an index location is s3://bucket/prefix (letters, digits, . _ = -)")
    if server and bucket not in allowed_buckets():
        raise IndexError_(f"this service does not read the bucket {bucket} (CLAIMS_INDEX_BUCKETS)")
    return "s3", bucket, prefix


def dataset_location(location: str, dataset_id: str) -> str:
    return f"{location.rstrip('/')}/datasets/{_safe(dataset_id)}"


FETCH_WORKERS = int(os.environ.get("CLAIMS_INDEX_FETCH_WORKERS") or 32)
_S3 = None
_S3_LOCK = threading.Lock()
_POOL = None


def _s3():
    """One S3 client for the process: boto3 clients are safe to share between threads once made, and making
    one per file costs more than reading a small file. Its connection pool fits FETCH_WORKERS."""
    global _S3
    if _S3 is None:
        with _S3_LOCK:
            if _S3 is None:
                import boto3
                from botocore.config import Config

                _S3 = boto3.client("s3", config=Config(max_pool_connections=FETCH_WORKERS + 4,
                                                       retries={"max_attempts": 5, "mode": "standard"}))
    return _S3


def _pool():
    """The threads that read datasets into the cache: shared by every request and the prewarm, so the reads in
    flight never exceed FETCH_WORKERS however many requests wait on them."""
    global _POOL
    if _POOL is None:
        with _S3_LOCK:
            if _POOL is None:
                from concurrent.futures import ThreadPoolExecutor

                _POOL = ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="index-fetch")
    return _POOL


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def upload(local: Path, location: str, name: str) -> str | None:
    """Put one file of a dataset's index at `location` (the dataset's own location); its S3 version id."""
    scheme, where, prefix = parse(location, server=False)
    if scheme == "file":
        dest = Path(where) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, dest)
        return None
    with open(local, "rb") as fh:
        out = _s3().put_object(Bucket=where, Key=f"{prefix}/{name}", Body=fh)
    return out.get("VersionId")


def download(location: str, name: str, version_id: str | None, dest: Path) -> None:
    """Read one file of a dataset's index (the version recorded at publish, when the bucket keeps versions)."""
    scheme, where, prefix = parse(location)
    if scheme == "file":
        shutil.copyfile(Path(where) / name, dest)
        return
    kwargs = {"Bucket": where, "Key": f"{prefix}/{name}"}
    if version_id:
        kwargs["VersionId"] = version_id
    body = _s3().get_object(**kwargs)["Body"]
    with open(dest, "wb") as fh:
        shutil.copyfileobj(body, fh, 1 << 20)


class Cache:
    """The local copy of indexes the service serves from: one directory per dataset, written whole after
    every file matched its recorded sha256 and size, so a reader never sees an unchecked file."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        (self.root / "datasets").mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def dir(self, dataset_id: str) -> Path:
        return self.root / "datasets" / _safe(dataset_id)

    def ensure(self, dataset_id: str, entry: dict) -> Path:
        """The dataset's checked local copy, read from its location the first time."""
        final = self.dir(dataset_id)
        if (final / ".checked").exists():
            return final
        with self._guard:
            lock = self._locks.setdefault(dataset_id, threading.Lock())
        with lock:
            if (final / ".checked").exists():
                return final
            tmp = Path(tempfile.mkdtemp(dir=self.root, prefix=".fetch-"))
            try:
                for name, f in sorted(entry["files"].items()):
                    if name not in FILES:
                        raise IndexError_(f"{dataset_id}: {name} is not an index file")
                    download(entry["location"], name, f.get("version_id"), tmp / name)
                    size, sha = (tmp / name).stat().st_size, sha256_file(tmp / name)
                    if size != f["size"] or sha != f["sha256"]:
                        raise IndexError_(f"{dataset_id}: {name} at {entry['location']} is not the file recorded at "
                                          f"publish (sha256 {sha[:12]}, expected {f['sha256'][:12]}); refusing to serve it")
                (tmp / ".checked").write_text("")
                shutil.rmtree(final, ignore_errors=True)
                os.replace(tmp, final)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return final

    def ensure_many(self, entries: list[tuple[str, dict]]) -> None:
        """Every dataset's checked local copy, reading the missing ones in parallel (FETCH_WORKERS at a time).
        A cross-trait query names about 1,400 datasets: read one after another from S3 they took over a
        minute, past the gateway's 60 s; in parallel they take a few seconds. The first error is raised once
        every read has finished, so no read is left writing behind a failed request."""
        missing = [(d, e) for d, e in entries if not (self.dir(d) / ".checked").exists()]
        if not missing:
            return
        if len(missing) == 1:
            self.ensure(*missing[0])
            return
        futures = [_pool().submit(self.ensure, d, e) for d, e in missing]
        errors = [f.exception() for f in futures]
        first = next((e for e in errors if e is not None), None)
        if first is not None:
            raise first
