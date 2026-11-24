"""CLI sign-in by device code (RFC 8628) and the publisher tokens it issues (service/device_login.py,
docs/cli-login.md)."""
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from service import admin_gateway, device_login
from service import db as service_db
from service.app import app

SECRET = "test-gateway-secret-at-least-32-characters"
ADMIN = "approver@example.org"
client = TestClient(app)


@pytest.fixture(autouse=True)
def _gateway(monkeypatch):
    monkeypatch.setattr(admin_gateway, "SECRET", SECRET)
    # Each test starts with no pending codes, so the caps count only its own.
    con = service_db.connect()
    with con:
        con.execute("DELETE FROM device_codes")
    con.close()


def _assertion(purpose, email=ADMIN):
    now = int(time.time())
    return {"Authorization": "Bearer " + jwt.encode(
        {"iss": "claims-admin", "aud": "claims-api", "iat": now, "exp": now + 120, "jti": str(uuid.uuid4()),
         "sub": email, "purpose": purpose, "email": email, "email_verified": True}, SECRET, algorithm="HS256")}


def _rows(sql, params=()):
    con = service_db.connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _exec(sql, params=()):
    con = service_db.connect()
    with con:
        con.execute(sql, params)
    con.close()


def _start(host="pigean-runner", ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    r = client.post("/v1/auth/device", json={"client": "claims-cli/test", "host": host}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _poll(device_code):
    return client.post("/v1/auth/device/token", json={"device_code": device_code})


def _unthrottle(device_code):
    """Pretend the last poll was long ago, so the next one is not answered slow_down."""
    _exec("UPDATE device_codes SET last_polled_at = NULL WHERE device_code_sha256 = ?",
          (device_login._hash(device_code),))


def _login(host="pigean-runner"):
    started = _start(host)
    code = started["user_code"]
    assert client.post(f"/v1/admin/api/device/{code}/approve", headers=_assertion("device_approve")).status_code == 200
    r = _poll(started["device_code"])
    assert r.status_code == 200, r.text
    return r.json()


def test_a_sign_in_waits_for_approval_then_issues_one_token():
    started = _start()
    assert set(started) == {"device_code", "user_code", "verification_uri", "verification_uri_complete",
                            "expires_in", "interval"}
    assert started["expires_in"] == 600 and started["interval"] == 5
    code = started["user_code"]
    assert len(code) == 9 and code[4] == "-" and set(code.replace("-", "")) <= set(device_login.USER_ALPHABET)
    assert started["verification_uri_complete"] == started["verification_uri"] + "?code=" + code

    first = _poll(started["device_code"])
    assert first.status_code == 400 and first.json()["error"] == "authorization_pending"
    assert first.headers["cache-control"] == "no-store"
    # Polling faster than the interval is slow_down.
    assert _poll(started["device_code"]).json()["error"] == "slow_down"

    # The console sees the request, then approves it as the assertion's email.
    seen = client.get(f"/v1/admin/api/device/{code.lower()}", headers=_assertion("device_approve"))
    assert seen.status_code == 200, seen.text
    assert seen.json()["host"] == "pigean-runner" and seen.json()["client"] == "claims-cli/test"
    approved = client.post(f"/v1/admin/api/device/{code.replace('-', '')}/approve", headers=_assertion("device_approve"))
    assert approved.status_code == 200 and approved.json()["status"] == "approved"

    _unthrottle(started["device_code"])
    issued = _poll(started["device_code"])
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert body["token_type"] == "Bearer" and body["email"] == ADMIN and body["expires_in"] == 30 * 24 * 3600
    assert device_login.TOKEN_RE.match(body["access_token"]) and device_login.TOKEN_ID_RE.match(body["token_id"])
    assert issued.headers["cache-control"] == "no-store"

    # Exactly once: the code is consumed.
    _unthrottle(started["device_code"])
    again = _poll(started["device_code"])
    assert again.status_code == 400 and again.json()["error"] == "expired_token"
    # Only hashes are stored.
    stored = _rows("SELECT token_sha256, email, host FROM publisher_tokens WHERE token_id = ?", (body["token_id"],))
    assert stored == [{"token_sha256": device_login._hash(body["access_token"]), "email": ADMIN, "host": "pigean-runner"}]
    assert not _rows("SELECT 1 FROM device_codes WHERE device_code_sha256 = ?", (started["device_code"],))


def test_denied_expired_and_unknown_codes():
    started = _start()
    assert client.post(f"/v1/admin/api/device/{started['user_code']}/deny",
                       headers=_assertion("device_approve")).status_code == 200
    assert _poll(started["device_code"]).json()["error"] == "access_denied"
    # A decided code cannot be decided again or looked up.
    assert client.post(f"/v1/admin/api/device/{started['user_code']}/approve",
                       headers=_assertion("device_approve")).status_code == 404
    assert client.get(f"/v1/admin/api/device/{started['user_code']}", headers=_assertion("device_approve")).status_code == 404

    late = _start()
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    _exec("UPDATE device_codes SET expires_at = ? WHERE device_code_sha256 = ?", (past, device_login._hash(late["device_code"])))
    assert client.post(f"/v1/admin/api/device/{late['user_code']}/approve",
                       headers=_assertion("device_approve")).status_code == 404
    assert _poll(late["device_code"]).json()["error"] == "expired_token"

    assert _poll("x" * 43).json()["error"] == "invalid_grant"
    assert _poll("not a code").json()["error"] == "invalid_grant"
    assert client.get("/v1/admin/api/device/AEIOU123", headers=_assertion("device_approve")).status_code == 404


def test_only_a_device_approve_assertion_can_see_or_decide_a_sign_in():
    started = _start()
    code = started["user_code"]
    for purpose in ("admin_read", "admin_write", "auth_event"):
        assert client.get(f"/v1/admin/api/device/{code}", headers=_assertion(purpose)).status_code == 403
        assert client.post(f"/v1/admin/api/device/{code}/approve", headers=_assertion(purpose)).status_code == 403
    assert client.post(f"/v1/admin/api/device/{code}/approve").status_code == 401
    # The approver is the assertion's email, whatever a body says.
    r = client.post(f"/v1/admin/api/device/{code}/approve", headers=_assertion("device_approve", "Real@Example.org"),
                    json={"email": "someone-else@example.org"})
    assert r.status_code == 200
    assert _rows("SELECT decided_by FROM device_codes WHERE user_code = ?", (code.replace("-", ""),)) == [
        {"decided_by": "real@example.org"}]


def test_whoami_logout_and_revocation_refuse_with_one_answer():
    token = _login()
    bearer = {"Authorization": "Bearer " + token["access_token"]}
    me = client.get("/v1/auth/whoami", headers=bearer)
    assert me.status_code == 200 and me.json()["email"] == ADMIN and me.json()["token_id"] == token["token_id"]
    assert _rows("SELECT last_used_at FROM publisher_tokens WHERE token_id = ?", (token["token_id"],))[0]["last_used_at"]

    refused = [client.get("/v1/auth/whoami"),
               client.get("/v1/auth/whoami", headers={"Authorization": "Bearer clp_" + "A" * 43}),
               client.get("/v1/auth/whoami", headers={"Authorization": "Bearer " + token["access_token"][:-1]}),
               client.get("/v1/auth/whoami", headers={"X-API-Key": token["access_token"]})]
    assert client.post("/v1/auth/logout", headers=bearer).json() == {"revoked": True, "token_id": token["token_id"]}
    refused.append(client.get("/v1/auth/whoami", headers=bearer))

    expired = _login()
    _exec("UPDATE publisher_tokens SET expires_at = '2000-01-01 00:00:00' WHERE token_id = ?", (expired["token_id"],))
    refused.append(client.get("/v1/auth/whoami", headers={"Authorization": "Bearer " + expired["access_token"]}))

    revoked = _login()
    assert client.post(f"/v1/admin/api/tokens/{revoked['token_id']}/revoke", headers=_assertion("admin_write")).status_code == 200
    assert client.post(f"/v1/admin/api/tokens/{revoked['token_id']}/revoke", headers=_assertion("admin_write")).status_code == 409
    assert client.post(f"/v1/admin/api/tokens/{revoked['token_id']}/revoke", headers=_assertion("admin_read")).status_code == 403
    refused.append(client.get("/v1/auth/whoami", headers={"Authorization": "Bearer " + revoked["access_token"]}))

    for r in refused:
        assert r.status_code == 401, r.text
        assert r.json() == {"detail": device_login.UNAUTHORIZED}
        assert r.headers["www-authenticate"] == "Bearer"

    listed = client.get("/v1/admin/api/tokens", headers=_assertion("admin_read"))
    assert listed.status_code == 200
    states = {t["tokenId"]: t["state"] for t in listed.json()["tokens"]}
    assert states[token["token_id"]] == "revoked" and states[expired["token_id"]] == "expired"
    assert states[revoked["token_id"]] == "revoked"
    assert "clp_" not in listed.text and "sha256" not in listed.text


def test_require_publisher_guards_a_route():
    from fastapi import FastAPI

    guarded = FastAPI()

    @guarded.get("/publish")
    def publish(publisher: dict = Depends(device_login.require_publisher)):
        return publisher

    c = TestClient(guarded)
    assert c.get("/publish").status_code == 401
    token = _login()
    got = c.get("/publish", headers={"Authorization": "Bearer " + token["access_token"]}).json()
    assert got["email"] == ADMIN and got["token_id"] == token["token_id"]


def test_pending_codes_are_capped_per_address_and_the_address_is_the_proxys():
    # A client-sent X-Forwarded-For entry sits left of what the proxy appends and is ignored.
    for _ in range(device_login.MAX_PENDING_PER_IP):
        _start(ip="34.1.2.3, 52.1.2.9")
    r = client.post("/v1/auth/device", json={"host": "h"}, headers={"X-Forwarded-For": "1.1.1.1, 52.1.2.9"})
    assert r.status_code == 429 and r.headers["retry-after"] == "600"
    # Another address may still start one, and private proxy hops are skipped.
    started = _start(ip="52.1.2.10, 10.0.3.4")
    assert _rows("SELECT ip FROM device_codes WHERE device_code_sha256 = ?",
                 (device_login._hash(started["device_code"]),)) == [{"ip": "52.1.2.10"}]


def test_client_reported_text_is_bounded_and_printable():
    assert client.post("/v1/auth/device", json={"host": "a‮b"}).status_code == 400
    assert client.post("/v1/auth/device", json={"host": "x" * 256}).status_code == 422
    assert client.post("/v1/auth/device", json={}).status_code == 200


def test_every_step_is_audited():
    token = _login(host="audited-host")
    client.post("/v1/auth/logout", headers={"Authorization": "Bearer " + token["access_token"]})
    events = [r["event"] for r in _rows("SELECT event FROM admin_auth_events WHERE detail LIKE ? ORDER BY id",
                                        ("%audited-host%",))]
    assert events == ["device_login_requested", "device_login_approved", "publisher_token_issued",
                      "publisher_token_revoked"]


def test_the_table_browser_never_lists_the_hash_tables():
    tables = client.get("/v1/admin/api/tables?fresh=true", headers=_assertion("admin_read")).json()["tables"]
    names = {t["name"] for t in tables}
    assert "device_codes" not in names and "publisher_tokens" not in names
    assert client.get("/v1/admin/api/tables/publisher_tokens/rows", headers=_assertion("admin_read")).status_code in (400, 404)


def test_the_contract_lists_the_public_auth_routes():
    contract = client.get("/v1/openapi.yaml").text
    for path in ("/v1/auth/device:", "/v1/auth/device/token:", "/v1/auth/whoami:", "/v1/auth/logout:", "PublisherToken:"):
        assert path in contract
