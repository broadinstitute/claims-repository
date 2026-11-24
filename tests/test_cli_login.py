"""`claims login`, `whoami` and `logout` against a real HTTP server running this service (claims_cli/auth.py)."""
import socket
import stat
import threading
import time

import pytest
import uvicorn

from claims_cli import auth
from claims_cli import main as cli
from service import db as service_db
from service import device_login
from service.app import app


@pytest.fixture(scope="module")
def api():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CLAIMS_TOKEN", raising=False)
    monkeypatch.delenv("CLAIMS_API", raising=False)
    con = service_db.connect()
    with con:
        con.execute("DELETE FROM device_codes")
    con.close()


def _approve(user_code, email="cli-approver@example.org"):
    con = service_db.connect()
    try:
        with con:
            assert device_login.decide(con, device_login.normalize_user_code(user_code), email, True)
    finally:
        con.close()


def _code(output):
    return output.split("and confirm ", 1)[1].split()[0]


def test_login_waits_for_approval_then_whoami_and_logout(api, capsys, monkeypatch):
    approved = []

    def sleep(_seconds):  # the first wait is when the admin approves
        if not approved:
            _approve(_code(capsys.readouterr().out))
            approved.append(True)
    monkeypatch.setattr(auth.time, "sleep", sleep)
    cli.main(["login", "--api", api + "/v1/"])
    out = capsys.readouterr().out
    assert "Signed in to " + api + " as cli-approver@example.org" in out and "clp_" not in out

    path = auth.credentials_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    saved = auth._load()["apis"][api]
    assert saved["token"].startswith("clp_") and saved["email"] == "cli-approver@example.org"

    cli.main(["whoami", "--api", api])
    assert "cli-approver@example.org at " + api in capsys.readouterr().out
    cli.main(["logout", "--api", api])
    assert "Revoked " + saved["token_id"] in capsys.readouterr().out
    assert api not in auth._load()["apis"]
    with pytest.raises(SystemExit, match="not signed in"):
        cli.main(["whoami", "--api", api])


def test_no_wait_then_continue(api, capsys, monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda _s: None)
    cli.main(["login", "--api", api, "--no-wait"])
    out = capsys.readouterr().out
    assert "claims login --continue --api " + api in out
    pending = auth._load()["pending"][api]
    assert pending["user_code"] == _code(out)
    assert stat.S_IMODE(auth.credentials_path().stat().st_mode) == 0o600

    _approve(pending["user_code"], "agent-owner@example.org")
    cli.main(["login", "--api", api, "--continue"])
    assert "as agent-owner@example.org" in capsys.readouterr().out
    assert api not in auth._load()["pending"]
    with pytest.raises(SystemExit, match="no sign-in is waiting"):
        cli.main(["login", "--api", api, "--continue"])


def test_a_denied_login_saves_nothing(api, capsys, monkeypatch):
    def sleep(_seconds):
        con = service_db.connect()
        with con:
            device_login.decide(con, device_login.normalize_user_code(_code(capsys.readouterr().out)), "x@example.org", False)
        con.close()
    monkeypatch.setattr(auth.time, "sleep", sleep)
    with pytest.raises(SystemExit, match="denied"):
        cli.main(["login", "--api", api])
    assert not auth.credentials_path().exists()


def test_claims_token_overrides_the_file(api, capsys, monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda _s: None)
    cli.main(["login", "--api", api, "--no-wait"])
    _approve(auth._load()["pending"][api]["user_code"], "env-user@example.org")
    cli.main(["login", "--api", api, "--continue"])
    token = auth._load()["apis"][api]["token"]
    capsys.readouterr()

    monkeypatch.setenv("CLAIMS_TOKEN", "clp_" + "A" * 43)
    with pytest.raises(SystemExit, match="expired or revoked"):
        cli.main(["whoami", "--api", api])
    monkeypatch.setenv("CLAIMS_TOKEN", token)
    monkeypatch.setenv("CLAIMS_API", api)
    cli.main(["whoami"])
    assert "env-user@example.org" in capsys.readouterr().out and auth.token_for(api) == token


def test_only_https_or_this_machine():
    for bad in ("http://api.example.org/api/claims", "http://localhost.evil.example", "http://127.0.0.1.nip.io",
                "http://localhost@evil.example", "https://user@x.example.org", "ftp://x.example.org"):
        with pytest.raises(SystemExit, match="not an https URL"):
            auth.api_base(bad)
    assert auth.api_base("http://localhost:8110") == "http://localhost:8110"
    assert auth.api_base(None) == auth.QA
    assert auth.api_base("https://x.example.org/api/claims/v1/") == "https://x.example.org/api/claims"
