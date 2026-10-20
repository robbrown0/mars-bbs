# Tests and validation

All recovery tests use new containers, networks, volumes and credentials. They do not mount or contact production databases.

Run the deterministic suites:

```console
docker build --target test -t mars-hotel-bbs-test .
docker run --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  mars-hotel-bbs-test python3 -m unittest discover -s tests -v
docker run --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  mars-hotel-bbs-test python3 tests/run-models.py
```

Validated on 2026-10-20:

- all Python modules compiled and imported; 16 unit tests passed
- 14 recovered model programs passed, including 5,000 bounded game commands and nine door model tests
- Argon2id authentication, ticket replay rejection, UID-bound sessions and administrator boundaries passed
- board CRUD, private mail isolation/migration, chat, file, all five native games and exact Stockfish 19 execution passed
- SSH guest rejection, account login, arcade/talk navigation, logout and remote-command denial passed
- real service RPC integration passed for auth, sessions, boards, mail, files, chat, games, information and internal TLS IRC
- external IRC parsing, local capability authentication, quiet-user presence, relay, sanitization, TLS, reconnect and NickServ refresh passed against local fixtures
- deterministic information parsing, cache controls, SSRF allowlist and terminal-control removal passed
- linked-BBS mail passed real pinned-TLS delivery, HMAC authentication, replay and duplicate rejection, sender and peer rate limits, queue status, hidden route metadata and bad-certificate rejection
- isolated live collectors returned data from CISA, CelesTrak, EFF, Global Voices, NASA, NOAA, TVmaze, USGS and Wikipedia; market data correctly reported unavailable without its optional key
- Compose configuration, non-root identities, read-only filesystems, dropped capabilities and published-port policy were inspected
- a staged-tree Gitleaks scan found no secrets; repeat the scan on every release commit

The deployed custom OpenSSH 10.6p1 source was unavailable. The Debian OpenSSH 10.0p1 replacement passed the complete SSH smoke test. The exact deployed Stockfish 19 binary was recovered reproducibly from the checksum-pinned official release archive.

The optional door image rebuilds from the checksum-pinned dosemu2 artifact retained on its Launchpad build record and the exact FDPP, libfdpp and comcom64 PPA packages. The recovered artifact's `dosemu2.bin` SHA-256 matches the deployed binary. The build verifies and extracts the public LORD 4.02 archive. An isolated fresh world accepted the included registration and created the registered marker. Door authority and game-selection tests pass, and a private export of the exact deployed image was preserved. Full interactive Zork play remains outside the public test suite because its story data is not included.
