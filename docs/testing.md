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

Validated on 2026-10-18:

- all 37 Python modules compiled and imported; 10 recovery unit tests passed
- 13 recovered model programs passed, including 5,000 bounded game commands and nine door model tests
- Argon2id authentication, ticket replay rejection, UID-bound sessions and administrator boundaries passed
- board CRUD, private mail isolation/migration, chat, file, all five native games and exact Stockfish 19 execution passed
- SSH guest rejection, account login, arcade/talk navigation, logout and remote-command denial passed
- real service RPC integration passed for auth, sessions, boards, mail, files, chat, games, information and internal TLS IRC
- external IRC parsing, local capability authentication, quiet-user presence, relay, sanitization, TLS, reconnect and NickServ refresh passed against local fixtures
- deterministic information parsing, cache controls, SSRF allowlist and terminal-control removal passed
- isolated live collectors returned data from CISA, CelesTrak, EFF, Global Voices, NASA, NOAA, TVmaze, USGS and Wikipedia; market data correctly reported unavailable without its optional key
- Compose configuration, non-root identities, read-only filesystems, dropped capabilities and published-port policy were inspected
- a staged-tree Gitleaks scan found no secrets; repeat the scan on every release commit

The deployed custom OpenSSH 10.6p1 source was unavailable. The Debian OpenSSH 10.0p1 replacement passed the complete SSH smoke test. The exact deployed Stockfish 19 binary was recovered reproducibly from the checksum-pinned official release archive.

The optional door source image does not currently rebuild because its exact historical dosemu2/FDPP PPA versions are no longer published. Door authority and game-selection tests pass, and a private export of the exact deployed image was preserved. Full interactive play of proprietary game data is outside the public test suite.
