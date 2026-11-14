"""Who may call the admin API: the claims admin console's server, only.

The console (/admin in frontend/, a Next.js app on Vercel) signs users in with Google and
admits approved admins. Its SERVER then calls /v1/admin/api/* with a
short-lived HS256 assertion signed with CLAIMS_ADMIN_GATEWAY_SECRET, the
secret both deployments share (the reveal gateway pattern). Browsers never
hold this credential, and the API never trusts an identity a browser sends.

An assertion must carry:
  iss  CLAIMS_ADMIN_GATEWAY_ISSUER    (default claims-admin)
  aud  CLAIMS_ADMIN_GATEWAY_AUDIENCE  (default claims-api)
  iat, exp (lifetime at most MAX_LIFETIME seconds), jti, sub
  purpose  "admin_read" for the read API, "admin_write" for the write routes
           (collection registration, deposit keys), "auth_event" for the
           sign-in audit
For admin_read and admin_write: email + email_verified true, and the email
must also be in this deployment's CLAIMS_ADMIN_EMAILS (checked here as well
as in the console, so the API's own list is authoritative). A purpose is
never accepted in place of another: a read assertion cannot call a write
route, nor a write assertion a read route. The registration routes are
additionally off unless the API process sets CLAIMS_COLLECTION_ROOTS
(service/admin.py); the deposit-key routes are not.
"""
from __future__ import annotations

import os

import jwt
from fastapi import HTTPException
from starlette.requests import Request

MAX_LIFETIME = 600
MIN_SECRET = 32


def _env_set(name):
    return {e.strip().lower() for e in os.environ.get(name, "").split(",") if e.strip()}


ADMIN_EMAILS = _env_set("CLAIMS_ADMIN_EMAILS")
SECRET = os.environ.get("CLAIMS_ADMIN_GATEWAY_SECRET", "")
ISSUER = os.environ.get("CLAIMS_ADMIN_GATEWAY_ISSUER", "claims-admin")
AUDIENCE = os.environ.get("CLAIMS_ADMIN_GATEWAY_AUDIENCE", "claims-api")


def _claims(request: Request, purpose: str) -> dict:
    if len(SECRET) < MIN_SECRET:
        raise HTTPException(status_code=503, detail="The admin API is not configured on this deployment.")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Admin assertion required.")
    try:
        claims = jwt.decode(
            header[7:].strip(), SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
            options={"require": ["exp", "iat", "jti", "sub"]}, leeway=30,
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired admin assertion.")
    if claims["exp"] - claims["iat"] > MAX_LIFETIME:
        raise HTTPException(status_code=401, detail="Admin assertion lifetime is too long.")
    if claims.get("purpose") != purpose:
        raise HTTPException(status_code=403, detail="Admin assertion is not valid for this operation.")
    return claims


def _admin(request: Request, purpose: str) -> dict:
    claims = _claims(request, purpose)
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True or email not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="This account is not an approved admin for this API.")
    return {"email": email, "name": claims.get("name")}


def require_admin(request: Request) -> dict:
    """FastAPI dependency for the read API: an approved, verified admin."""
    return _admin(request, "admin_read")


def require_admin_write(request: Request) -> dict:
    """FastAPI dependency for the write routes: the same admin checks under
    their own purpose, so a leaked read assertion cannot publish."""
    return _admin(request, "admin_write")


def require_auth_event(request: Request) -> dict:
    """FastAPI dependency for the console's sign-in audit writes."""
    return _claims(request, "auth_event")
