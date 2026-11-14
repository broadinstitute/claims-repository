#!/usr/bin/env python3
"""Issue, list and revoke deposit keys (service/api_keys.py).

POST /v1/deposits requires a depositor's key in `X-API-Key`. A key names one
depositor (an agent, a pipeline, a client), and every bundle it sends is
recorded as a deposit by that key.

  scripts/api_key.py issue "REVEAL agent service (QA)" --by you@example.org
  scripts/api_key.py list
  scripts/api_key.py revoke key_0123456789abcdef --by you@example.org

`issue` prints the key once; only its SHA-256 is stored, so it cannot be shown
again. Give it to the depositor over a private channel. Revoking stops new
deposits at once and keeps the key's past deposits attributed to it.

Runs against whichever backend the CLAIMS_* env selects: the SQLite file
(CLAIMS_DB, default ./claims.db), or MySQL when CLAIMS_MYSQL_HOST is set. QA
and prod share one MySQL database, so a key issued there works in both; to
write to MySQL, add --database <name> to the issue or revoke command so a
stray environment variable cannot aim this at it by accident.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from service import api_keys, db  # noqa: E402


def target() -> str:
    return f"MySQL {db.MYSQL_HOST}/{db.MYSQL_DATABASE}" if db.MYSQL_HOST else f"SQLite {db.DB_PATH}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="Create a key and print it once")
    issue.add_argument("name", help="Who deposits with this key, e.g. 'REVEAL agent service (QA)'")
    issue.add_argument("--by", help="Who is issuing it (recorded)")
    issue.add_argument("--database", help="Required for MySQL: the database name you mean to change")
    commands.add_parser("list", help="List keys with their deposit counts")
    revoke = commands.add_parser("revoke", help="Stop a key depositing")
    revoke.add_argument("key_id")
    revoke.add_argument("--by", help="Who is revoking it (recorded)")
    revoke.add_argument("--database", help="Required for MySQL: the database name you mean to change")
    args = parser.parse_args(argv)

    writes = args.command != "list"
    if db.MYSQL_HOST and writes and args.database != db.MYSQL_DATABASE:
        print(f"Refusing to change {target()}: add --database {db.MYSQL_DATABASE} after the {args.command} arguments to confirm.", file=sys.stderr)
        return 2
    print(f"Database: {target()}", file=sys.stderr)
    con = db.connect()
    try:
        if args.command == "list":
            keys = api_keys.list_keys(con)
            if not keys:
                print("No keys. Every deposit is refused until one is issued.")
            for key in keys:
                state = f"revoked {key['revoked_at']}" if key["revoked_at"] else "active"
                print(f"{key['key_id']}  {state:<28}  {key['deposits']:>6} deposits  last {key['last_deposit_at'] or '-':<19}  {key['name']}")
            return 0
        try:
            with con:
                if args.command == "issue":
                    issued = api_keys.issue(con, args.name, args.by)
                else:
                    revoked = api_keys.revoke(con, args.key_id, args.by)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.command == "issue":
            print(f"key_id: {issued['key_id']}")
            print(f"name:   {issued['name']}")
            print(f"key:    {issued['key']}")
            print("Send it as the X-API-Key header on POST /v1/deposits. It cannot be shown again.", file=sys.stderr)
            return 0
        if not revoked:
            print(f"error: {args.key_id} is not an active key", file=sys.stderr)
            return 1
        print(f"revoked {args.key_id}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
