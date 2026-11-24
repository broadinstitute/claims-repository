"""claims login / whoami / logout: device-code sign-in to a deployed claims API (docs/cli-login.md).

    claims login [--api URL]             prints a link and a code, waits for approval, saves the token
    claims login --no-wait               prints the link and code and returns at once
    claims login --continue              waits for that code's approval and saves the token
    claims whoami [--api URL]
    claims logout [--api URL]

Tokens are kept in ~/.config/claims/credentials.json (or $XDG_CONFIG_HOME/claims), mode 0600, one per API
base URL. CLAIMS_TOKEN, when set, is used instead of the file (unattended runs). A token is never printed.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

QA = "https://api-qa.hugeampkpnbi.org/api/claims"
CLIENT = "claims-cli/1"


def api_base(value: str | None) -> str:
    """The API base URL: --api, else $CLAIMS_API, else QA; without a trailing slash or /v1."""
    base = (value or os.environ.get("CLAIMS_API") or QA).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        url = urllib.parse.urlsplit(base)
        url.port  # noqa: B018  (raises on a malformed port)
    except ValueError:
        sys.exit(f"claims: {base} is not a URL.")
    local = url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
    if (url.scheme != "https" and not local) or not url.hostname or url.username or url.password:
        sys.exit(f"claims: {base} is not an https URL. Tokens are only sent over https (or to this machine).")
    return base


def credentials_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "claims" / "credentials.json"


def _load() -> dict:
    path = credentials_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1, "apis": {}, "pending": {}}
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"claims: cannot read {path}: {exc}. Remove it and run `claims login` again.")
    data.setdefault("apis", {})
    data.setdefault("pending", {})
    return data


def _save(data: dict) -> None:
    """Write the credentials file readable by its owner only: created 0600 (never chmod-ed after the fact), then
    moved into place."""
    path = credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".credentials.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def token_for(api: str) -> str | None:
    """The token to send to `api`: CLAIMS_TOKEN, else the saved one."""
    return os.environ.get("CLAIMS_TOKEN") or (_load()["apis"].get(api) or {}).get("token")


def _call(method: str, url: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json", "User-Agent": CLIENT}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        sys.exit(f"claims: cannot reach {url}: {getattr(exc, 'reason', exc)}")


def _detail(body: dict) -> str:
    detail = body.get("detail") or body.get("error_description") or body.get("error")
    return detail if isinstance(detail, str) else json.dumps(body)[:300]


def _start(api: str) -> dict:
    status, body = _call("POST", f"{api}/v1/auth/device", {"client": CLIENT, "host": socket.gethostname()[:255]})
    if status != 200:
        sys.exit(f"claims: could not start a sign-in at {api} ({status}): {_detail(body)}")
    return {"device_code": body["device_code"], "user_code": body["user_code"],
            "verification_uri": body["verification_uri"], "verification_uri_complete": body["verification_uri_complete"],
            "interval": int(body.get("interval") or 5), "expires_at": time.time() + int(body.get("expires_in") or 600)}


def _instructions(api: str, pending: dict) -> None:
    minutes = max(1, round((pending["expires_at"] - time.time()) / 60))
    print(f"To sign in to {api}:\n"
          f"  open        {pending['verification_uri_complete']}\n"
          f"  and confirm {pending['user_code']}\n"
          f"Sign in there with an admin Google account and approve. The code expires in {minutes} minutes.",
          flush=True)


def _wait(api: str, pending: dict) -> dict:
    """Poll until the code is approved (the token), or ends."""
    interval = pending["interval"]
    print("waiting for approval...", flush=True)
    while True:
        if time.time() > pending["expires_at"]:
            sys.exit("claims: the code expired before it was approved. Run `claims login` again.")
        time.sleep(interval)
        status, body = _call("POST", f"{api}/v1/auth/device/token", {"device_code": pending["device_code"]})
        if status == 200:
            return body
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            sys.exit("claims: the sign-in was denied in the console.")
        if error in ("expired_token", "invalid_grant"):
            sys.exit("claims: the code expired or was already used. Run `claims login` again.")
        sys.exit(f"claims: sign-in failed ({status}): {_detail(body)}")


def _finish(api: str, issued: dict) -> None:
    data = _load()
    data["apis"][api] = {"token": issued["access_token"], "token_id": issued["token_id"], "email": issued["email"],
                         "expires_at": issued.get("expires_at"),
                         "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    data["pending"].pop(api, None)
    _save(data)
    print(f"Signed in to {api} as {issued['email']} ({issued['token_id']}). "
          f"The token expires {(issued.get('expires_at') or '')[:10]} and is saved in {credentials_path()}.")
    if os.environ.get("CLAIMS_TOKEN"):
        print("note: CLAIMS_TOKEN is set, so commands send it instead of this token until you unset it.")


def cmd_login(args) -> None:
    api = api_base(args.api)
    if args.no_wait and args.resume:
        sys.exit("claims: use either --no-wait or --continue")
    if args.resume:
        pending = _load()["pending"].get(api)
        if not pending:
            sys.exit(f"claims: no sign-in is waiting for {api}. Run `claims login --no-wait` first.")
        print(f"Continuing the sign-in with code {pending['user_code']}.", flush=True)
        _finish(api, _wait(api, pending))
        return
    pending = _start(api)
    _instructions(api, pending)
    if args.no_wait:
        data = _load()
        data["pending"][api] = pending
        _save(data)
        print("After approving, run:  claims login --continue" + (f" --api {api}" if args.api else ""))
        return
    try:
        issued = _wait(api, pending)
    except KeyboardInterrupt:
        sys.exit("\nclaims: sign-in stopped. The code expires on its own.")
    _finish(api, issued)


def cmd_whoami(args) -> None:
    api = api_base(args.api)
    token = token_for(api)
    if not token:
        sys.exit(f"claims: not signed in to {api}. Run `claims login`.")
    status, body = _call("GET", f"{api}/v1/auth/whoami", token=token)
    if status == 401:
        sys.exit(f"claims: the token for {api} is expired or revoked. Run `claims login`.")
    if status != 200:
        sys.exit(f"claims: whoami failed ({status}): {_detail(body)}")
    source = "CLAIMS_TOKEN" if os.environ.get("CLAIMS_TOKEN") else str(credentials_path())
    print(f"{body['email']} at {api}\n  token {body['token_id']}, expires {body['expires_at']} (from {source})")


def cmd_logout(args) -> None:
    api = api_base(args.api)
    data = _load()
    saved = data["apis"].get(api)
    token = os.environ.get("CLAIMS_TOKEN") or (saved or {}).get("token")
    if not token:
        sys.exit(f"claims: not signed in to {api}.")
    status, body = _call("POST", f"{api}/v1/auth/logout", token=token)
    if status == 200:
        print(f"Revoked {body.get('token_id')} at {api}.")
    elif status == 401:
        print(f"The token for {api} was already expired or revoked.")
    else:
        sys.exit(f"claims: logout failed ({status}): {_detail(body)}. The token was kept; try again.")
    if saved and (not os.environ.get("CLAIMS_TOKEN") or saved.get("token") == token):
        data["apis"].pop(api, None)
        _save(data)
        print(f"Removed it from {credentials_path()}.")
    if os.environ.get("CLAIMS_TOKEN"):
        print("note: CLAIMS_TOKEN is still set in this shell; unset it.")


def add_commands(sub) -> None:
    p = sub.add_parser("login", help="sign in to a deployed claims API with a device code")
    p.add_argument("--api", help=f"the API's base URL (default: $CLAIMS_API, else {QA})")
    p.add_argument("--no-wait", action="store_true", help="print the link and code and return; finish with --continue")
    p.add_argument("--continue", dest="resume", action="store_true", help="finish a sign-in started with --no-wait")
    p.set_defaults(func=cmd_login)
    p = sub.add_parser("whoami", help="who this machine is signed in as")
    p.add_argument("--api", help="the API's base URL (default: $CLAIMS_API, else QA)")
    p.set_defaults(func=cmd_whoami)
    p = sub.add_parser("logout", help="revoke this machine's token and forget it")
    p.add_argument("--api", help="the API's base URL (default: $CLAIMS_API, else QA)")
    p.set_defaults(func=cmd_logout)
