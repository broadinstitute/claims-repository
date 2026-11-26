"""Deposit keys: who may write, and who wrote what.

POST /v1/deposits is the only external write path. A depositor (an agent, a
pipeline, a person's client) holds one key and sends it as `X-API-Key`. The
service stores only the key's SHA-256, a public key id and the name given at
issue, and records every accepted bundle as a deposit: which key sent it, when,
and which objects it carried. Reads stay open, so a key id and its name are
public; the key and its hash never leave this module.

Keys are issued, listed and revoked here (scripts/api_key.py is the CLI).

A `claims login` (a publisher token, service/device_login.py) may write too:
`require_writer` accepts either credential and returns one actor dict,
{"kind": "key", "key_id", "name"} or {"kind": "login", "name", "email",
"token_id"}. A login deposit is recorded under its token id and email
(deposits.publisher_token_id, publisher_email; schema 012), never a key id.
Publicly it is attributed to "claims login" only; the email and token id are
shown in the admin console.
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


COLLECTION_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


def grant(con, key_id: str, collection: str, granted_by: str | None = None) -> bool:
    """Let a key deposit into a collection. False when the grant already exists."""
    if not COLLECTION_KEY_RE.match(collection or ""):
        raise ValueError("collection must be a collection key such as dismech")
    if con.execute("SELECT 1 FROM api_key_grants WHERE key_id = ? AND collection_key = ?", (key_id, collection)).fetchone():
        return False
    con.execute(f"INSERT INTO api_key_grants (key_id, collection_key, granted_at, granted_by) VALUES (?, ?, {con.dialect.now}, ?)",
                (key_id, collection, _actor(granted_by)))
    return True


def revoke_grant(con, key_id: str, collection: str) -> bool:
    return con.execute("DELETE FROM api_key_grants WHERE key_id = ? AND collection_key = ?",
                       (key_id, collection)).rowcount > 0


def grants(con, key_id: str) -> list[str]:
    return [r["collection_key"] for r in con.execute(
        "SELECT collection_key FROM api_key_grants WHERE key_id = ? ORDER BY collection_key", (key_id,))]


def has_grant(con, depositor: dict, collection: str) -> bool:
    """Whether the key may write into the collection. Read inside the write's transaction as a locking read, like
    still_valid: a grant withdrawn before this point stops the write; one withdrawn after it waits for the write.
    A login has no per-collection grants: any approved login may write any collection, as it may publish one
    (docs/cli-login.md)."""
    if is_login(depositor):
        return True
    return con.execute(f"SELECT 1 FROM api_key_grants WHERE key_id = ? AND collection_key = ?{con.dialect.lock_share}",
                       (depositor["key_id"], collection)).fetchone() is not None


def list_keys(con) -> list[dict]:
    rows = con.execute(
        "SELECT k.key_id, k.name, k.created_at, k.created_by, k.revoked_at, k.revoked_by, "
        "(SELECT COUNT(*) FROM deposits d WHERE d.key_id = k.key_id) AS deposits, "
        "(SELECT MAX(d.received_at) FROM deposits d WHERE d.key_id = k.key_id) AS last_deposit_at "
        "FROM api_keys k ORDER BY k.created_at, k.key_id"
    ).fetchall()
    held: dict[str, list[str]] = {}
    for r in con.execute("SELECT key_id, collection_key FROM api_key_grants ORDER BY collection_key"):
        held.setdefault(r["key_id"], []).append(r["collection_key"])
    return [{**{name: (str(row[name]) if name.endswith("_at") and row[name] is not None else row[name])
                for name in ("key_id", "name", "created_at", "created_by", "revoked_at", "revoked_by", "deposits",
                             "last_deposit_at")}, "collections": held.get(row["key_id"], [])}
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


LOGIN_NAME = "claims login"  # the public name of every login's deposits
BOTH_CREDENTIALS = "Send either X-API-Key or Authorization: Bearer, not both."


def is_login(actor: dict) -> bool:
    return actor.get("kind") == "login"


def require_writer(request: Request) -> dict:
    """FastAPI dependency for the routes that write into an object collection (open, deposit, close): a deposit key
    (`X-API-Key`) or a `claims login` token (`Authorization: Bearer clp_...`). A bearer token that is malformed,
    unknown, revoked or expired is 401, never treated as no credential. Runs before the body is read."""
    from . import device_login

    key = request.headers.get(HEADER)
    bearer = device_login._bearer(request)
    if key and bearer is not None:
        raise HTTPException(status_code=400, detail=BOTH_CREDENTIALS)
    if bearer is None:
        depositor = require_depositor(request)
        return {"kind": "key", "key_id": depositor["key_id"], "name": depositor["name"]}
    if not bearer.startswith("clp_"):  # not a login token at all (a key sent the wrong way, say): the key's 401
        raise HTTPException(status_code=401, detail=UNAUTHORIZED, headers={"WWW-Authenticate": "ApiKey"})
    con = db.connect()
    try:
        publisher = device_login.authenticate(con, bearer)
    finally:
        con.close()
    if publisher is None:
        raise HTTPException(status_code=401, detail=device_login.UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    return {"kind": "login", "name": LOGIN_NAME, "email": publisher["email"], "token_id": publisher["token_id"]}


def receipt_depositor(actor: dict) -> dict:
    """The depositor a deposit's receipt names: a key as before ({key_id, name}); a login as its kind, public
    name, and (the caller's own) email and token id."""
    if is_login(actor):
        return {"kind": "login", "name": LOGIN_NAME, "email": actor["email"], "token_id": actor["token_id"]}
    return {"key_id": actor["key_id"], "name": actor["name"]}


def unauthorized(actor: dict) -> HTTPException:
    """The 401 for a credential that stopped being valid during a write."""
    if is_login(actor):
        from . import device_login

        return HTTPException(status_code=401, detail=device_login.UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    return HTTPException(status_code=401, detail=UNAUTHORIZED, headers={"WWW-Authenticate": "ApiKey"})


def still_valid(con, depositor: dict) -> bool:
    """The credential re-check inside the deposit transaction, made after the transaction's own writes and just
    before the deposit is recorded. It is a locking read (MySQL: FOR SHARE, which sees the latest committed row
    whatever the transaction's snapshot; SQLite: this transaction already holds the write lock), so a key or login
    revoked before this point deposits nothing, and a revocation arriving after it waits until the deposit has
    committed. A login must also not have expired."""
    if is_login(depositor):
        from . import device_login

        row = con.execute(f"SELECT revoked_at, expires_at FROM publisher_tokens WHERE token_id = ?"
                          f"{con.dialect.lock_share}", (depositor["token_id"],)).fetchone()
        return (row is not None and row["revoked_at"] is None
                and device_login._parse(row["expires_at"]) > device_login._now())
    row = con.execute(
        f"SELECT revoked_at FROM api_keys WHERE key_id = ?{con.dialect.lock_share}", (depositor["key_id"],)
    ).fetchone()
    return row is not None and row["revoked_at"] is None


def new_deposit_id() -> str:
    """`dep_` + 32 hex: milliseconds since the epoch, then random. Ids therefore sort by time, which orders
    deposits received within one second (received_at has second precision on both backends)."""
    return "dep_" + format(int(time.time() * 1000), "012x")[-12:] + secrets.token_hex(10)


def writer_columns(actor: dict) -> tuple[str | None, str | None, str | None]:
    """(key id, token id, email) to record for a write: exactly one of a key id or a login's token id (with its
    email). Anything else is refused, so no deposit or release is ever recorded without its writer."""
    if is_login(actor):
        token_id, email = actor.get("token_id"), actor.get("email")
        if not (isinstance(token_id, str) and token_id and isinstance(email, str) and email) or actor.get("key_id"):
            raise ValueError("a login writes with its token id and email, and no key id")
        return None, token_id, email
    key_id = actor.get("key_id")
    if actor.get("kind", "key") != "key" or not (isinstance(key_id, str) and key_id) or actor.get("token_id"):
        raise ValueError("a write is recorded under a key id or a login's token id")
    return key_id, None, None


def record_deposit(con, depositor: dict, deposited: list[dict], edges=(), collection: str | None = None,
                   release: str | None = None) -> str:
    """Record one accepted bundle inside its own transaction: the key, the time, every object it carried (whether
    this deposit created the object or it already existed) and the document's edges, which have no ids of their own
    and so belong to the deposit that carried them."""
    key_id, token_id, email = writer_columns(depositor)
    deposit_id = new_deposit_id()
    objects = {}
    for item in deposited:  # an id repeated in one bundle is one object; its first outcome stands
        # `differs`: the object existed and this deposit's copy differed in slots identity ignores. The stored
        # document was kept; the record shows that this depositor held a different one.
        objects.setdefault(item["id"], "differs" if item.get("stored_differs") else item["status"])
    con.execute(
        f"INSERT INTO deposits (deposit_id, key_id, publisher_token_id, publisher_email, received_at, object_count, "
        f"collection_key, release_version) VALUES (?, ?, ?, ?, {con.dialect.now}, ?, ?, ?)",
        (deposit_id, key_id, token_id, email, len(objects), collection, release),
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


def depositor_of(row, admin: bool = False) -> dict:
    """Who made a deposit, from its row: a key {key_id, name}; a login {kind: login, name: "claims login"}, with its
    email and token id only for the admin console."""
    if row["key_id"] is None:
        out = {"kind": "login", "name": LOGIN_NAME}
        if admin:
            out.update(email=row["publisher_email"], token_id=row["publisher_token_id"])
        return out
    return {"key_id": row["key_id"], "name": row["name"]}


def _deposit(row, admin: bool = False) -> dict:
    return {"deposit_id": row["deposit_id"], "received_at": str(row["received_at"]), "object_count": row["object_count"],
            "depositor": depositor_of(row, admin),
            "collection": ({"key": row["collection_key"], "release": row["release_version"],
                            "release_id": row["release_id"], "state": row["release_state"]}
                           if row["collection_key"] else None)}


_DEPOSIT_SELECT = ("SELECT d.deposit_id, d.received_at, d.object_count, d.key_id, k.name, d.publisher_token_id, "
                   "d.publisher_email, d.collection_key, d.release_version, r.release_id, r.state AS release_state "
                   "FROM deposits d LEFT JOIN api_keys k ON k.key_id = d.key_id "
                   "LEFT JOIN object_releases r ON r.collection_key = d.collection_key AND r.version = d.release_version")


def depositor_id_ok(value) -> bool:
    """A `deposited_by` filter: a key id or a login's token id."""
    from .device_login import TOKEN_ID_RE

    return isinstance(value, str) and bool(KEY_ID_RE.match(value) or TOKEN_ID_RE.match(value))


DEPOSITED_BY_SQL = "(d.key_id = ? OR d.publisher_token_id = ?)"


def deposits(con, *, object_id=None, deposited_by=None, limit=50, offset=0, admin: bool = False) -> dict:
    """Deposits, newest first: all of them, those that carried one object, or those one key or login sent."""
    where, params = [], []
    if object_id is not None:
        where.append("EXISTS (SELECT 1 FROM deposit_objects o WHERE o.deposit_id = d.deposit_id AND o.object_id = ?)")
        params.append(object_id)
    if deposited_by is not None:
        where.append(DEPOSITED_BY_SQL)
        params += [deposited_by, deposited_by]
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = con.execute(f"SELECT COUNT(*) AS c FROM deposits d{clause}", params).fetchone()["c"]
    rows = con.execute(
        f"{_DEPOSIT_SELECT}{clause} ORDER BY d.received_at DESC, d.deposit_id DESC LIMIT ? OFFSET ?", [*params, limit, offset]
    ).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "deposits": [_deposit(row, admin) for row in rows]}


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
