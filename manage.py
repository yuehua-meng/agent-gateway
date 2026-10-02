"""Local maintenance; never prints existing secrets."""
import argparse
import secrets
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("new-key", help="generate a new random key; does not change config")
    backup = sub.add_parser("backup", help="online SQLite backup")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "new-key":
        print(secrets.token_urlsafe(32))
    else:
        source = ROOT / "data" / "gateway.db"
        destination = args.destination.resolve()
        if destination.exists() or destination == source.resolve():
            raise SystemExit("Destination must be a new file.")
        if not source.exists():
            raise SystemExit("Start the gateway once before backing up.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
        print("Backup saved:", destination)


if __name__ == "__main__":
    main()
