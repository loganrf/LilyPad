"""Maintenance commands, run inside the container:

    docker compose exec lilypad python -m app.cli reset-password
    docker compose exec lilypad python -m app.cli reset-password --password "my new password"
    docker compose exec -T lilypad python -m app.cli backup > lilypad-backup.sqlite3
    docker compose run --rm -T lilypad python -m app.cli restore < lilypad-backup.sqlite3
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
import tempfile

from . import db, settings_store
from .config import load_config
from .security import generate_password, hash_password


def reset_password(password: str | None) -> int:
    config = load_config()
    if config.admin_password:
        print("LILYPAD_ADMIN_PASSWORD is set, so it controls the admin password. "
              "Change or remove it in docker-compose.yml / .env and restart.", file=sys.stderr)
        return 1
    if password is None:
        password = generate_password()
        print(f"New admin password: {password}")
    elif len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1
    db.init_db(config.db_path)
    conn = db.connect(config.db_path)
    try:
        settings_store.save(conn, {"admin_password_hash": hash_password(password)})
    finally:
        conn.close()
    try:
        config.password_file.unlink()
    except OSError:
        pass
    print("Admin password updated. Existing admin sessions are signed out.")
    return 0


REQUIRED_TABLES = {"settings", "feedings", "diapers", "notes", "vital_params", "vital_checks", "reminders"}


def backup() -> int:
    """Write a consistent snapshot of the database to stdout."""
    config = load_config()
    if not config.db_path.exists():
        print(f"No database at {config.db_path}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "backup.sqlite3")
        src, dest = db.connect(config.db_path), sqlite3.connect(out)
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        with open(out, "rb") as f:
            sys.stdout.buffer.write(f.read())
    sys.stdout.flush()
    return 0


def restore(source: str) -> int:
    """Replace the database with a backup. Stop the running app first."""
    config = load_config()
    data = sys.stdin.buffer.read() if source == "-" else open(source, "rb").read()
    if not data.startswith(b"SQLite format 3\x00"):
        print("That isn't a SQLite database file.", file=sys.stderr)
        return 1
    config.data_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.data_dir, suffix=".restore")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        check = sqlite3.connect(tmp)
        try:
            tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            check.close()
        if not REQUIRED_TABLES <= tables or not ok:
            print("That database isn't a valid LilyPad backup.", file=sys.stderr)
            return 1
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(str(config.db_path) + suffix)
            except OSError:
                pass
        os.replace(tmp, config.db_path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    db.init_db(config.db_path)  # apply any newer migrations
    print(f"Restored {len(data)} bytes into {config.db_path}.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="LilyPad maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-password", help="set a new admin password")
    group = reset.add_mutually_exclusive_group()
    group.add_argument("--password", help="the new password (otherwise one is generated)")
    group.add_argument("--prompt", action="store_true", help="type the new password interactively")
    sub.add_parser("backup", help="write a consistent database snapshot to stdout")
    rest = sub.add_parser("restore", help="replace the database with a backup (stop the app first)")
    rest.add_argument("source", nargs="?", default="-", help="backup file, or - for stdin (default)")
    args = parser.parse_args(argv)
    if args.command == "backup":
        return backup()
    if args.command == "restore":
        return restore(args.source)
    if args.command == "reset-password":
        password = args.password
        if args.prompt:
            password = getpass.getpass("New admin password: ")
            if password != getpass.getpass("Repeat: "):
                print("Passwords don't match.", file=sys.stderr)
                return 1
        return reset_password(password)
    return 2


if __name__ == "__main__":
    sys.exit(main())
