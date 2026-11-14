"""Every test session runs against a throwaway SQLite database.

service.db reads CLAIMS_DB once, when it is first imported. Point it at a
temp file before ANY test module imports the service, whatever order pytest
collects them in, and refuse to run against the repository's local
claims.db: a test that loads fixtures or deposits bundles must never write
into a developer's database.
"""
import os
import tempfile
from pathlib import Path

os.environ["CLAIMS_DB"] = os.path.join(tempfile.mkdtemp(prefix="claims-tests-"), "test-claims.db")
os.environ.pop("CLAIMS_MYSQL_HOST", None)

REAL_DB = (Path(__file__).resolve().parent.parent / "claims.db").resolve()


def pytest_sessionstart(session):
    from service import db

    assert Path(db.DB_PATH).resolve() != REAL_DB, "tests must not run against the local claims.db"


import hashlib  # noqa: E402
import secrets  # noqa: E402

import pytest  # noqa: E402

# One deposit key for the whole session. Tests swap service.db.DB_PATH to fresh databases, so the key is
# seeded into every SQLite database the suite connects to, the way an operator would have issued it there.
TEST_DEPOSITOR = {"key_id": "key_" + secrets.token_hex(8), "name": "test-suite depositor",
                  "key": "clm_" + secrets.token_urlsafe(32)}


def _seed_test_key(con):
    if con.execute("SELECT 1 FROM api_keys WHERE key_id = ?", (TEST_DEPOSITOR["key_id"],)).fetchone() is None:
        con.execute(
            "INSERT INTO api_keys (key_id, name, key_sha256, created_at, created_by) VALUES (?, ?, ?, datetime('now'), 'tests')",
            (TEST_DEPOSITOR["key_id"], TEST_DEPOSITOR["name"], hashlib.sha256(TEST_DEPOSITOR["key"].encode()).hexdigest()),
        )
        con.commit()


@pytest.fixture(scope="session")
def depositor():
    """The session's deposit key: {key_id, name, key}."""
    return dict(TEST_DEPOSITOR)


@pytest.fixture(scope="session", autouse=True)
def _deposits_are_keyed(request):
    """Deposits need a key. Every test module's shared `client` sends the session key by default, so existing
    deposit calls keep working; a test of the unauthenticated path builds its own TestClient."""
    from service import db

    real_connect = db.connect

    def connect(path=None):
        con = real_connect(path)
        if con.dialect.sqlite:
            _seed_test_key(con)
        return con

    db.connect = connect
    seen = set()
    for item in request.session.items:
        client = getattr(item.module, "client", None)
        if client is not None and id(client) not in seen:
            seen.add(id(client))
            client.headers["X-API-Key"] = TEST_DEPOSITOR["key"]
    yield
    db.connect = real_connect
