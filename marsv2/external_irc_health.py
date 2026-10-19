"""Health check for the reconnecting external IRC adapter process."""

from __future__ import annotations

import argparse
import json
import os
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--maximum-age", type=int, default=180)
    args = parser.parse_args()
    info = os.stat(args.state_file, follow_symlinks=False)
    if not info.st_size or time.time() - info.st_mtime > args.maximum_age:
        raise SystemExit(1)
    with open(args.state_file, encoding="utf-8") as stream:
        state = json.load(stream)
    if state.get("version") != 1 or state.get("status") not in {
            "starting", "online", "offline"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
