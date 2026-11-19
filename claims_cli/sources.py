"""Reading result files where they live: listing, versioning, fetching, and the
pins that record exactly which bytes a release read.

Only S3 is implemented (through the aws CLI, so no SDK is needed). A pin is
(location, size, ETag, version id when the bucket is versioned, sha256 once
the file has been read). Pins live beside the index in `sources/pins.jsonl`;
they are not part of any DAPPER record, so pinning changes no id.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class SourceError(RuntimeError):
    pass


def split_s3(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise SourceError(f"{uri}: only s3:// sources are supported")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def _aws(*args: str) -> str:
    out = subprocess.run(["aws", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise SourceError(f"aws {' '.join(args[:3])} failed: {out.stderr.strip()[:400]}")
    return out.stdout


@dataclass
class Listed:
    location: str
    size: int
    etag: str
    last_modified: str
    version_id: str | None = None


def versioning(bucket: str) -> str:
    """'Enabled', 'Suspended' or 'Off' (never enabled)."""
    raw = _aws("s3api", "get-bucket-versioning", "--bucket", bucket, "--output", "json").strip()
    return (json.loads(raw).get("Status") if raw else None) or "Off"


def list_prefix(uri: str, versioned: bool = False) -> list[Listed]:
    """Every object under an s3:// prefix (the CLI pages through all of them)."""
    bucket, prefix = split_s3(uri)
    if versioned:
        raw = json.loads(_aws("s3api", "list-object-versions", "--bucket", bucket, "--prefix", prefix,
                              "--output", "json") or "{}")
        items = [v for v in raw.get("Versions") or [] if v.get("IsLatest")]
    else:
        raw = json.loads(_aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix,
                              "--output", "json") or "{}")
        items = raw.get("Contents") or []
    return [Listed(f"s3://{bucket}/{o['Key']}", int(o["Size"]), o["ETag"].strip('"'), o["LastModified"],
                   o.get("VersionId")) for o in items]


def fetch(location: str, dest: Path, version_id: str | None = None, attempts: int = 4) -> None:
    """Download one object (a pinned version when given), retrying transient failures."""
    bucket, key = split_s3(location)
    args = ["s3api", "get-object", "--bucket", bucket, "--key", key, str(dest)]
    if version_id:
        args[5:5] = ["--version-id", version_id]
    for attempt in range(1, attempts + 1):
        try:
            _aws(*args)
            return
        except SourceError:
            if attempt == attempts:
                raise
            time.sleep(5 * attempt)


# --- path patterns ---------------------------------------------------------

def pattern_regex(path: str) -> re.Pattern:
    """`{kind}/{library}/portal/{trait}/{kind}.json` -> a regex with one named
    group per placeholder; a repeated placeholder must repeat its value."""
    out, seen, pos = "", set(), 0
    for m in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path):
        out += re.escape(path[pos:m.start()])
        name = m.group(1)
        out += f"(?P={name})" if name in seen else f"(?P<{name}>[^/]+)"
        seen.add(name)
        pos = m.end()
    return re.compile("^" + out + re.escape(path[pos:]) + "$")


def placeholders(path: str) -> list[str]:
    names = []
    for m in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


# --- pins ------------------------------------------------------------------

class Pins:
    """sources/pins.jsonl: one line per (location, etag) ever read. The last
    line for a key wins."""

    def __init__(self, index_dir: Path):
        self.path = Path(index_dir) / "sources" / "pins.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.by_key: dict[tuple[str, str], dict] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    p = json.loads(line)
                    self.by_key[(p["location"], p["etag"])] = p

    def sha256(self, location: str, etag: str) -> str | None:
        p = self.by_key.get((location, etag))
        return p.get("sha256") if p else None

    def record(self, listed: Listed, sha256: str | None) -> None:
        p = {"location": listed.location, "etag": listed.etag, "size": listed.size,
             "version_id": listed.version_id, "last_modified": listed.last_modified, "sha256": sha256,
             "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.by_key[(listed.location, listed.etag)] = p
        with open(self.path, "a") as fh:
            fh.write(json.dumps(p, sort_keys=True) + "\n")
