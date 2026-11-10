"""Reference extraction for the object_refs index: which dapper objects a
document points at. One definition shared by the loader, deposit, claim
synthesis, and the audit, so the index can never disagree with itself."""
from __future__ import annotations

import re

# Digests are base64url for DAPPER-ID-1 (sha512t24u: may contain '-' / '_')
# and lowercase hex for the local-dev profile.
DAPPER_ID_RE = re.compile(r"^dapper:[A-Za-z]+\.[A-Za-z0-9_-]+\Z")
LOCAL_DEV_ID_RE = re.compile(r"^dapper:[A-Za-z]+\.local[0-9a-f]{32}\Z")
MAX_ID_LENGTH = 128  # dapper_objects.id / object_refs columns are VARCHAR(128)


def dapper_refs(payload: dict) -> set[str]:
    """Every dapper ID referenced anywhere in the document's values, excluding
    the document's own `id`."""
    found: set[str] = set()

    def walk(value):
        if isinstance(value, str):
            if DAPPER_ID_RE.match(value):
                found.add(value)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)

    walk({k: v for k, v in payload.items() if k != "id"})
    return found


def rebuild_object_refs(con, batch: int = 5000) -> int:
    """Recompute object_refs from every stored document (backfill after a
    migration, or repair). Idempotent; returns the number of refs written."""
    import json

    written, pending = 0, []
    rows = con.execute("SELECT id, payload FROM dapper_objects").fetchall()
    for row in rows:
        for ref in dapper_refs(json.loads(row["payload"])):
            if len(ref) <= MAX_ID_LENGTH:  # longer refs cannot be stored or resolved
                pending.append((row["id"], ref))
        if len(pending) >= batch:
            con.executemany(con.dialect.insert_ref, pending)
            written += len(pending)
            pending = []
    if pending:
        con.executemany(con.dialect.insert_ref, pending)
        written += len(pending)
    return written
