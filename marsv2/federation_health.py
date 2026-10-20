"""Local TLS health probe for the BBS mail-exchange container."""

from __future__ import annotations

import http.client
import ssl


def main() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection("127.0.0.1", 2443, timeout=2, context=context)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = response.read(1024)
        if response.status != 200 or b'"status":"ok"' not in body:
            raise SystemExit(1)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
