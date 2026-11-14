"""Deposit keys: who may write, and who wrote what.

POST /v1/deposits is the only external write path. A depositor (an agent, a
pipeline, a person's client) holds one key and sends it as `X-API-Key`. The
service stores only the key's SHA-256, a public key id and the name given at
issue, and records every accepted bundle as a deposit: which key sent it, when,
and which objects it carried. Reads stay open, so a key id and its name are
public; the key and its hash never leave this module.

Keys are issued, listed and revoked here (scripts/api_key.py is the CLI).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time

from fastapi import HTTPException, Request

from . import db

KEY_RE = re.compile(r"clm_[A-Za-z0-9_-]{43}\Z")
KEY_ID_RE = re.compile(r"key_[0-9a-f]{16}\Z")
DEPOSIT_ID_RE = re.compile(r"dep_[0-9a-f]{32}\Z")
MAX_NAME = 120
MAX_ACTOR = 320
MAX_EDGE_END = 512  # indexed copy of an edge's endpoints; the full edge is in payload
HEADER = "X-API-Key"
# One answer for a missing, malformed, unknown or revoked key: the caller learns nothing about which.
UNAUTHORIZED = "A valid X-API-Key is required to deposit."


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _name(value) -> str:
    # The name is public and shown in the console: printable characters only (no controls, no bidi overrides).
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_NAME or not value.isprintable():
        raise ValueError(f"name must be 1-{MAX_NAME} printable characters")
    return value.strip()


def _actor(value) -> str | None:
    return value.strip()[:MAX_ACTOR] if isinstance(value, str) and value.strip() else None


def issue(con, name: str, created_by: str | None = None) -> dict:
    """Create a key. The returned `key` is shown once; only its hash is stored. No commit: the caller owns
    the transaction."""
    name = _name(name)
    key = "clm_" + secrets.token_urlsafe(32)
    key_id = "key_" + secrets.token_hex(8)
    con.execute(
        f"INSERT INTO api_keys (key_id, name, key_sha256, created_at, created_by) VALUES (?, ?, ?, {con.dialect.now}, ?)",
        (key_id, name, _hash(key), _actor(created_by)),
    )
    return {"key_id": key_id, "name": name, "key": key}


def revoke(con, key_id: str, revoked_by: str | None = None) -> bool:
    """Stop a key depositing. Its past deposits stay attributed to it. True when this call revoked it."""
    if not isinstance(key_id, str) or not KEY_ID_RE.match(key_id):
        raise ValueError("key_id must look like key_0123456789abcdef")
    cur = con.execute(
        f"UPDATE api_keys SET revoked_at = {con.dialect.now}, revoked_by = ? WHERE key_id = ? AND revoked_at IS NULL",
        (_actor(revoked_by), key_id),
    )
    return cur.rowcount == 1


def list_keys(con) -> list[dict]:
    rows = con.execute(
        "SELECT k.key_id, k.name, k.created_at, k.created_by, k.revoked_at, k.revoked_by, "
        "(SELECT COUNT(*) FROM deposits d WHERE d.key_id = k.key_id) AS deposits, "
        "(SELECT MAX(d.received_at) FROM deposits d WHERE d.key_id = k.key_id) AS last_deposit_at "
        "FROM api_keys k ORDER BY k.created_at, k.key_id"
    ).fetchall()
    return [{name: (str(row[name]) if name.endswith("_at") and row[name] is not None else row[name])
             for name in ("key_id", "name", "created_at", "created_by", "revoked_at", "revoked_by", "deposits", "last_deposit_at")}
            for row in rows]


def authenticate(con, key) -> dict | None:
    """The depositor a key names, or None. Looked up by hash, so the key itself is never compared or stored."""
    if not isinstance(key, str) or not KEY_RE.match(key):
        return None
    digest = _hash(key)
    row = con.execute(
        "SELECT key_id, name, key_sha256, revoked_at FROM api_keys WHERE key_sha256 = ?", (digest,)
    ).fetchone()
    if row is None or row["revoked_at"] is not None or not hmac.compare_digest(str(row["key_sha256"]), digest):
        return None
    return {"key_id": row["key_id"], "name": row["name"]}


def require_depositor(request: Request) -> dict:
    """FastAPI dependency for the deposit route. It runs before the body is read, so an unauthenticated
    caller gets 401 whatever it sent."""
    key = request.headers.get(HEADER)
    depositor = None
    if key:
        con = db.connect()
        try:
            depositor = authenticate(con, key)
        finally:
            con.close()
    if depositor is None:
        raise HTTPException(status_code=401, detail=UNAUTHORIZED, headers={"WWW-Authenticate": "ApiKey"})
    return depositor


def still_valid(con, depositor: dict) -> bool:
    """The key re-check inside the deposit transaction, made after the transaction's own writes and just before the
    deposit is recorded. It is a locking read (MySQL: FOR SHARE, which sees the latest committed row whatever the
    transaction's snapshot; SQLite: this transaction already holds the write lock), so a key revoked before this
    point deposits nothing, and a revocation arriving after it waits until the deposit has committed."""
    row = con.execute(
        f"SELECT revoked_at FROM api_keys WHERE key_id = ?{con.dialect.lock_share}", (depositor["key_id"],)
    ).fetchone()
    return row is not None and row["revoked_at"] is None


def new_deposit_id() -> str:
    """`dep_` + 32 hex: milliseconds since the epoch, then random. Ids therefore sort by time, which orders
    deposits received within one second (received_at has second precision on both backends)."""
    return "dep_" + format(int(time.time() * 1000), "012x")[-12:] + secrets.token_hex(10)


def record_deposit(con, depositor: dict, deposited: list[dict], edges=()) -> str:
    """Record one accepted bundle inside its own transaction: the key, the time, every object it carried (whether
    this deposit created the object or it already existed) and the document's edges, which have no ids of their own
    and so belong to the deposit that carried them."""
    deposit_id = new_deposit_id()
    objects = {}
    for item in deposited:  # an id repeated in one bundle is one object; its first outcome stands
        # `differs`: the object existed and this deposit's copy differed in slots identity ignores. The stored
        # document was kept; the record shows that this depositor held a different one.
        objects.setdefault(item["id"], "differs" if item.get("stored_differs") else item["status"])
    con.execute(
        f"INSERT INTO deposits (deposit_id, key_id, received_at, object_count) VALUES (?, ?, {con.dialect.now}, ?)",
        (deposit_id, depositor["key_id"], len(objects)),
    )
    con.executemany(
        "INSERT INTO deposit_objects (deposit_id, object_id, status) VALUES (?, ?, ?)",
        [(deposit_id, object_id, status) for object_id, status in objects.items()],
    )
    if edges:
        con.executemany(
            "INSERT INTO deposit_edges (deposit_id, seq, class_name, subject_id, object_id, payload) VALUES (?, ?, ?, ?, ?, ?)",
            [(deposit_id, seq, class_name, str(edge.get("subject", ""))[:MAX_EDGE_END], str(edge.get("object", ""))[:MAX_EDGE_END],
              json.dumps(edge, sort_keys=True, separators=(",", ":"), allow_nan=False))
             for seq, (class_name, edge) in enumerate(edges)],
        )
    return deposit_id


def _deposit(row) -> dict:
    return {"deposit_id": row["deposit_id"], "received_at": str(row["received_at"]), "object_count": row["object_count"],
            "depositor": {"key_id": row["key_id"], "name": row["name"]}}


_DEPOSIT_SELECT = ("SELECT d.deposit_id, d.received_at, d.object_count, d.key_id, k.name "
                   "FROM deposits d JOIN api_keys k ON k.key_id = d.key_id")


def deposits(con, *, object_id=None, deposited_by=None, limit=50, offset=0) -> dict:
    """Deposits, newest first: all of them, those that carried one object, or those one key sent."""
    where, params = [], []
    if object_id is not None:
        where.append("EXISTS (SELECT 1 FROM deposit_objects o WHERE o.deposit_id = d.deposit_id AND o.object_id = ?)")
        params.append(object_id)
    if deposited_by is not None:
        where.append("d.key_id = ?")
        params.append(deposited_by)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = con.execute(f"SELECT COUNT(*) AS c FROM deposits d{clause}", params).fetchone()["c"]
    rows = con.execute(
        f"{_DEPOSIT_SELECT}{clause} ORDER BY d.received_at DESC, d.deposit_id DESC LIMIT ? OFFSET ?", [*params, limit, offset]
    ).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "deposits": [_deposit(row) for row in rows]}


def deposit(con, deposit_id: str) -> dict | None:
    row = con.execute(f"{_DEPOSIT_SELECT} WHERE d.deposit_id = ?", (deposit_id,)).fetchone()
    if row is None:
        return None
    objects = con.execute(
        "SELECT o.object_id, o.status, x.class_name FROM deposit_objects o LEFT JOIN dapper_objects x ON x.id = o.object_id "
        "WHERE o.deposit_id = ? ORDER BY o.object_id", (deposit_id,)
    ).fetchall()
    edges = con.execute(
        "SELECT class_name, payload FROM deposit_edges WHERE deposit_id = ? ORDER BY seq", (deposit_id,)
    ).fetchall()
    return {**_deposit(row),
            "objects": [{"id": o["object_id"], "class_name": o["class_name"],
                         **({"status": "exists", "stored_differs": True} if o["status"] == "differs" else {"status": o["status"]})}
                        for o in objects],
            "edges": [{"class_name": e["class_name"], **json.loads(e["payload"])} for e in edges]}
