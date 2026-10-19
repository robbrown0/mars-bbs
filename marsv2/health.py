"""Container health probe for a selected V2 service."""

from __future__ import annotations

import os

from .client import call


def main() -> None:
    service = os.environ["MARS_V2_HEALTH_SERVICE"]
    result = call(service, "health")
    if result.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
