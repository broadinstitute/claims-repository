"""Device-code sign-in for the claims CLI, and the publisher tokens it issues.

The `claims` CLI often runs on a remote server, sometimes driven by an agent,
with no browser. It signs in with a device code (RFC 8628,
docs/cli-login.md):

  1. POST /v1/auth/device: the CLI gets a secret device code and a short user
     code, and shows the user code with the console's /admin/device link.
  2. An admin opens that page (Google sign-in, the console's ADMIN_EMAILS),
     sees the requesting host and IP, and approves or denies. The console's
     server calls the admin API with a `device_approve` assertion
     (service/admin.py), so the approver's email comes from the signed
     assertion, never from a request body.
  3. The CLI polls POST /v1/auth/device/token. Once approved, the code is
     exchanged, exactly once, for a publisher token (`clp_...`, 30 days).

The device code and the token are 32 random bytes each. Only their SHA-256 is
stored. A token is opaque, can be listed (never shown) and revoked from the
console, and revoked by its holder (`claims logout`). `require_publisher` is
the dependency publish routes use. Every request, decision, issue and
revocation is audited in admin_auth_events.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import db

USER_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"  # 20 consonants: no vowels, so no words; ~2.6e10 codes of 8
USER_CODE_RE = re.compile(f"[{USER_ALPHABET}]{{8}}\\Z")
DEVICE_CODE_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
TOKEN_RE = re.compile(r"clp_[A-Za-z0-9_-]{43}\Z")
TOKEN_ID_RE = re.compile(r"tok_[0-9a-f]{16}\Z")

DEVICE_TTL = 600            # seconds a device code may wait for a decision
INTERVAL = 5                # seconds between polls
TOKEN_TTL = 30 * 24 * 3600  # a publisher token's lifetime
MAX_PENDING_PER_IP = 5
MAX_PENDING = 100           # all IPs together: unauthenticated callers cannot fill the table
KEEP_DECIDED = timedelta(days=1)  # expired codes are deleted after this; the audit table keeps the history
MAX_HOST = 255
MAX_CLIENT = 64

VERIFICATION_URL = os.environ.get("CLAIMS_DEVICE_VERIFICATION_URL",
                                  "https://claims-repository.vercel.app/admin/device").rstrip("/")

# One answer for a missing, malformed, unknown, expired or revoked token: the caller learns nothing about which.
UNAUTHORIZED = "Sign in with `claims login`: a valid publisher token is required."
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class DeviceError(Exception):
    """An RFC 8628 token-endpoint error: authorization_pending, slow_down, access_denied, expired_token,
    invalid_grant."""

    def __init__(self, error: str, description: str):
        super().__init__(description)
        self.error, self.description = error, description


class TooManyPending(Exception):
    pass


# --- helpers -----------------------------------------------------------------------

def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _parse(value) -> datetime | None:
    """A stored time: a datetime from MySQL, text from SQLite."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")


def iso(value) -> str | None:
    t = _parse(value)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def normalize_user_code(raw) -> str | None:
    """The stored form of a user code: upper case, without the dash or spaces. None when it cannot be one."""
    if not isinstance(raw, str):
        return None
    code = re.sub(r"[\s-]", "", raw).upper()
    return code if USER_CODE_RE.match(code) else None


def format_user_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _label(value, limit: int, what: str) -> str | None:
    """Client-reported text (host, client): bounded and printable, or refused."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or len(value.strip()) > limit or not value.strip().isprintable():
        raise ValueError(f"{what} must be at most {limit} printable characters")
    return value.strip()


def client_ip(request: Request) -> str | None:
    """The caller's address: the rightmost public address in X-Forwarded-For plus the connecting peer. Proxies
    append the address they saw, so entries a client sends itself sit to the left and are skipped. Without a
    public address (local runs, tests) it is the connecting peer."""
    chain = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    peer = request.client.host if request.client else None
    hops = chain + ([peer] if peer else [])
    for hop in reversed(hops):
        try:
            address = ipaddress.ip_address(hop)
        except ValueError:
            continue
        if address.is_global:
            return str(address)
    return (peer or None) and peer[:64]


def _audit(con, event: str, email: str | None, detail: str) -> None:
    con.execute("INSERT INTO admin_auth_events (at, event, email, detail) VALUES (?, ?, ?, ?)",
                (_fmt(_now()), event, email, detail[:255]))


# --- the device code ---------------------------------------------------------------

def start(con, client: str | None, host: str | None, ip: str | None) -> dict:
    """Start a sign-in. The device code is returned once; only its hash is stored. No commit."""
    now = _now()
    client, host = _label(client, MAX_CLIENT, "client"), _label(host, MAX_HOST, "host")
    con.execute("DELETE FROM device_codes WHERE expires_at < ?", (_fmt(now - KEEP_DECIDED),))
    pending = "SELECT COUNT(*) AS n FROM device_codes WHERE status = 'pending' AND expires_at > ?"
    if con.execute(pending, (_fmt(now),)).fetchone()["n"] >= MAX_PENDING:
        raise TooManyPending("Too many sign-ins are waiting for approval. Try again in a few minutes.")
    if ip and con.execute(pending + " AND ip = ?", (_fmt(now), ip)).fetchone()["n"] >= MAX_PENDING_PER_IP:
        raise TooManyPending(f"{MAX_PENDING_PER_IP} sign-ins from this address are already waiting for approval. "
                             "Finish or let them expire (10 minutes), then try again.")
    for _ in range(10):
        user_code = "".join(secrets.choice(USER_ALPHABET) for _ in range(8))
        if con.execute("SELECT 1 FROM device_codes WHERE user_code = ?", (user_code,)).fetchone() is None:
            break
    else:  # pragma: no cover - 10 collisions in 2.6e10 codes
        raise RuntimeError("could not allocate a user code")
    device_code = secrets.token_urlsafe(32)
    con.execute(
        "INSERT INTO device_codes (device_code_sha256, user_code, status, client, host, ip, requested_at, expires_at) "
        "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)",
        (_hash(device_code), user_code, client, host, ip, _fmt(now), _fmt(now + timedelta(seconds=DEVICE_TTL))),
    )
    _audit(con, "device_login_requested", None, f"{format_user_code(user_code)} host={host or '-'} ip={ip or '-'} "
                                                f"client={client or '-'}")
    shown = format_user_code(user_code)
    return {"device_code": device_code, "user_code": shown, "verification_uri": VERIFICATION_URL,
            "verification_uri_complete": f"{VERIFICATION_URL}?code={shown}", "expires_in": DEVICE_TTL,
            "interval": INTERVAL}


def exchange(con, device_code) -> dict:
    """One poll of the token endpoint. Raises DeviceError until the code is approved; then issues the token, once.
    Commits its own writes (the caller passes a fresh connection)."""
    if not isinstance(device_code, str) or not DEVICE_CODE_RE.match(device_code):
        raise DeviceError("invalid_grant", "Unknown device code.")
    digest = _hash(device_code)
    now = _now()
    with con:
        row = con.execute("SELECT device_code_sha256, user_code, status, host, expires_at, last_polled_at, decided_by "
                          "FROM device_codes WHERE device_code_sha256 = ?", (digest,)).fetchone()
        if row is None or not hmac.compare_digest(str(row["device_code_sha256"]), digest):
            raise DeviceError("invalid_grant", "Unknown device code.")
        if row["status"] == "consumed" or _parse(row["expires_at"]) <= now:
            raise DeviceError("expired_token", "This sign-in expired or was already used. Run `claims login` again.")
        if row["status"] == "denied":
            raise DeviceError("access_denied", "The sign-in was denied in the console.")
        last = _parse(row["last_polled_at"])
        con.execute("UPDATE device_codes SET last_polled_at = ? WHERE device_code_sha256 = ?", (_fmt(now), digest))
        if last is not None and (now - last).total_seconds() < INTERVAL - 1:
            slow = DeviceError("slow_down", f"Poll at most every {INTERVAL} seconds.")
        elif row["status"] == "pending":
            slow = DeviceError("authorization_pending", "Waiting for approval in the console.")
        else:
            slow = None
        if slow is not None:
            con.commit()  # keep last_polled_at: `with` would roll it back on the exception
            raise slow
        # Approved: consume the code and issue the token in one transaction. The conditional update makes a
        # concurrent second poll find nothing to consume.
        token_id = "tok_" + secrets.token_hex(8)
        cur = con.execute("UPDATE device_codes SET status = 'consumed', token_id = ? "
                          "WHERE device_code_sha256 = ? AND status = 'approved'", (token_id, digest))
        if cur.rowcount != 1:
            raise DeviceError("expired_token", "This sign-in was already used. Run `claims login` again.")
        token = "clp_" + secrets.token_urlsafe(32)
        email, host = row["decided_by"], row["host"]
        expires = now + timedelta(seconds=TOKEN_TTL)
        con.execute("INSERT INTO publisher_tokens (token_id, token_sha256, email, host, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (token_id, _hash(token), email, host, _fmt(now), _fmt(expires)))
        _audit(con, "publisher_token_issued", email, f"{token_id} {format_user_code(row['user_code'])} "
                                                    f"host={host or '-'}")
    return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL, "email": email,
            "token_id": token_id, "expires_at": iso(expires)}


def pending(con, user_code: str) -> dict | None:
    """What the console shows before an admin decides: a pending, unexpired sign-in, or None."""
    row = con.execute("SELECT user_code, client, host, ip, requested_at, expires_at FROM device_codes "
                      "WHERE user_code = ? AND status = 'pending' AND expires_at > ?", (user_code, _fmt(_now()))).fetchone()
    if row is None:
        return None
    return {"userCode": format_user_code(row["user_code"]), "client": row["client"], "host": row["host"],
            "ip": row["ip"], "requestedAt": iso(row["requested_at"]), "expiresAt": iso(row["expires_at"])}


def decide(con, user_code: str, email: str, approve: bool) -> dict | None:
    """Approve or deny a pending sign-in as `email`. None when there is no pending, unexpired code. No commit."""
    request = pending(con, user_code)
    if request is None:
        return None
    now = _now()
    cur = con.execute("UPDATE device_codes SET status = ?, decided_by = ?, decided_at = ? "
                      "WHERE user_code = ? AND status = 'pending' AND expires_at > ?",
                      ("approved" if approve else "denied", email, _fmt(now), user_code, _fmt(now)))
    if cur.rowcount != 1:
        return None
    _audit(con, "device_login_approved" if approve else "device_login_denied", email,
           f"{request['userCode']} host={request['host'] or '-'} ip={request['ip'] or '-'}")
    return {**request, "status": "approved" if approve else "denied"}


# --- publisher tokens --------------------------------------------------------------

def authenticate(con, token) -> dict | None:
    """The publisher a token names, or None. Looked up by hash. Records its use. Commits."""
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return None
    digest = _hash(token)
    now = _now()
    with con:
        row = con.execute("SELECT token_id, token_sha256, email, host, expires_at, revoked_at FROM publisher_tokens "
                          "WHERE token_sha256 = ?", (digest,)).fetchone()
        if (row is None or not hmac.compare_digest(str(row["token_sha256"]), digest) or row["revoked_at"] is not None
                or _parse(row["expires_at"]) <= now):
            return None
        con.execute("UPDATE publisher_tokens SET last_used_at = ? WHERE token_id = ?", (_fmt(now), row["token_id"]))
    return {"email": row["email"], "token_id": row["token_id"], "host": row["host"],
            "expires_at": iso(row["expires_at"])}


def revoke(con, token_id: str, revoked_by: str | None) -> bool:
    """Revoke a token. True when this call revoked it. No commit."""
    if not isinstance(token_id, str) or not TOKEN_ID_RE.match(token_id):
        raise ValueError("token_id must look like tok_0123456789abcdef")
    cur = con.execute("UPDATE publisher_tokens SET revoked_at = ?, revoked_by = ? WHERE token_id = ? AND revoked_at IS NULL",
                      (_fmt(_now()), revoked_by, token_id))
    if cur.rowcount == 1:
        row = con.execute("SELECT email, host FROM publisher_tokens WHERE token_id = ?", (token_id,)).fetchone()
        _audit(con, "publisher_token_revoked", revoked_by, f"{token_id} of {row['email']} host={row['host'] or '-'}")
        return True
    return False


def list_tokens(con) -> list[dict]:
    """Every token, newest first: never the token or its hash."""
    now = _now()
    rows = con.execute("SELECT token_id, email, host, created_at, expires_at, last_used_at, revoked_at, revoked_by "
                       "FROM publisher_tokens ORDER BY created_at DESC, token_id LIMIT 500").fetchall()
    return [{"tokenId": r["token_id"], "email": r["email"], "host": r["host"], "createdAt": iso(r["created_at"]),
             "expiresAt": iso(r["expires_at"]), "lastUsedAt": iso(r["last_used_at"]), "revokedAt": iso(r["revoked_at"]),
             "revokedBy": r["revoked_by"],
             "state": "revoked" if r["revoked_at"] is not None else "expired" if _parse(r["expires_at"]) <= now
             else "active"} for r in rows]


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else None


def _publisher(request: Request) -> dict | None:
    token = _bearer(request)
    if not token:
        return None
    con = db.connect()
    try:
        return authenticate(con, token)
    finally:
        con.close()


async def require_publisher(request: Request) -> dict:
    """FastAPI dependency for publish routes: {email, token_id, host, expires_at} of a valid publisher token."""
    publisher = await run_in_threadpool(_publisher, request)
    if publisher is None:
        raise HTTPException(status_code=401, detail=UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    return publisher


# --- public routes -----------------------------------------------------------------

router = APIRouter(prefix="/v1/auth", tags=["Auth"])


class DeviceRequest(BaseModel):
    client: str | None = Field(None, max_length=MAX_CLIENT)
    host: str | None = Field(None, max_length=MAX_HOST)


class TokenRequest(BaseModel):
    device_code: str = Field(max_length=128)
    grant_type: str | None = Field(None, max_length=128)


def _connected(fn, *args):
    con = db.connect()
    try:
        return fn(con, *args)
    finally:
        con.close()


@router.post("/device")
async def device_start(body: DeviceRequest, request: Request):
    """Start a CLI sign-in: a device code for the CLI to poll with, and a user code for an admin to approve."""
    def run(con):
        with con:
            return start(con, body.client, body.host, client_ip(request))
    try:
        started = await run_in_threadpool(_connected, run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TooManyPending as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(DEVICE_TTL)})
    return JSONResponse(started, headers=NO_STORE)


@router.post("/device/token")
async def device_token(body: TokenRequest):
    """Poll a sign-in. 400 with an RFC 8628 `error` until it is approved; then the token, once."""
    try:
        issued = await run_in_threadpool(_connected, exchange, body.device_code)
    except DeviceError as exc:
        return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=400, headers=NO_STORE)
    return JSONResponse(issued, headers=NO_STORE)


@router.get("/whoami")
async def whoami(request: Request):
    """Who a publisher token signs in as, and until when."""
    publisher = await require_publisher(request)
    return JSONResponse({"email": publisher["email"], "token_id": publisher["token_id"],
                         "expires_at": publisher["expires_at"]}, headers=NO_STORE)


@router.post("/logout")
async def logout(request: Request):
    """Revoke the token this request carries."""
    publisher = await require_publisher(request)

    def run(con):
        with con:
            return revoke(con, publisher["token_id"], publisher["email"])
    await run_in_threadpool(_connected, run)
    return JSONResponse({"revoked": True, "token_id": publisher["token_id"]}, headers=NO_STORE)
