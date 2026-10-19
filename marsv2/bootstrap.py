"""One-shot, idempotent initialization of a fresh V2 account store."""

from __future__ import annotations

import argparse
import os

from .authd import Authority, _read_secret


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--admin-password-file", required=True)
    parser.add_argument("--owner", type=int, default=21001)
    args = parser.parse_args()
    authority = Authority(args.db)
    count = authority.db.execute("SELECT count(*) FROM accounts").fetchone()[0]
    if count == 0:
        authority.add_account("admin", _read_secret(args.admin_password_file),
                              "System Administrator", True)
    authority.db.close()
    root = os.path.dirname(args.db)
    for directory, subdirs, files in os.walk(root):
        os.chmod(directory, 0o700)
        os.chown(directory, args.owner, args.owner)
        for name in files:
            path = os.path.join(directory, name)
            os.chmod(path, 0o600)
            os.chown(path, args.owner, args.owner)
    print("V2 account store initialized")


if __name__ == "__main__":
    main()
