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

Validated on 2026-10-21:

- all Python modules compiled and imported; 22 unit tests passed
- 14 recovered model programs passed, including 5,000 bounded game commands and nine door model tests
- Argon2id authentication, ticket replay rejection, UID-bound sessions and administrator boundaries passed
- board CRUD, private mail isolation/migration, chat, file, all five native games and exact Stockfish 19 execution passed
- SSH guest rejection, account login, arcade/talk navigation, logout and remote-command denial passed
- real service RPC integration passed for auth, sessions, boards, mail, files, chat, games, information and internal TLS IRC
- external IRC parsing, local capability authentication, quiet-user presence, relay, sanitization, TLS, reconnect and NickServ refresh passed against local fixtures
- deterministic information parsing, cache controls, SSRF allowlist and terminal-control removal passed
- linked-BBS mail passed real pinned-TLS delivery, HMAC authentication, replay and duplicate rejection, sender and peer rate limits, queue status, hidden route metadata and bad-certificate rejection
- MarsAmp passed two-source catalog scanning, trash and symlink exclusion, role enforcement, path non-disclosure, curated-playlist resolution, no-repeat shuffle cycles, shared browser/terminal advancement, playback history, one-time session-bound browser pairing and authenticated HTTP byte-range streaming
- isolated live collectors returned data from CISA, CelesTrak, EFF, Global Voices, NASA, NOAA, TVmaze, USGS and Wikipedia; market data correctly reported unavailable without its optional key
- Compose configuration, non-root identities, read-only filesystems, dropped capabilities and published-port policy were inspected
- a staged-tree Gitleaks scan found no secrets; repeat the scan on every release commit

The deployed custom OpenSSH 10.6p1 source was unavailable. The Debian OpenSSH 10.0p1 replacement passed the complete SSH smoke test. The exact deployed Stockfish 19 binary was recovered reproducibly from the checksum-pinned official release archive.

The optional door image rebuilds from the checksum-pinned dosemu2 artifact retained on its Launchpad build record and the exact FDPP, libfdpp and comcom64 PPA packages. The recovered artifact's `dosemu2.bin` SHA-256 matches the deployed binary. The build verifies and extracts the public LORD 4.02 archive. An isolated fresh world accepted the included registration and created the registered marker. Door authority and game-selection tests pass, and a private export of the exact deployed image was preserved. Full interactive Zork play remains outside the public test suite because its story data is not included.


Validated on 2026-10-23 for the compact Lobby player and saved playback:

- 47 unit tests passed using synthetic data, including direct B/P/N controls,
  remapped O/E/S Lobby entries, role-gated player visibility, a real raw PTY,
  scrolling-title border alignment and the header's new row position
- saved per-account song, queue, shuffle bag and playback history survived a
  music-service restart; browser authorization was not restored
- playlists opened by default, with a T toggle to the full-catalog view
- generated browser-player JavaScript passed Node syntax checking
- deployed gateway/music source hashes matched the tested source; music RPC
  health passed and unauthenticated browser audio returned HTTP 401
- Gitleaks found no secrets in the staged change or existing Git history

The market credential was subsequently restored from an operator-supplied key
stored on persistent appdata and mounted read-only. Provider authentication and
a fresh four-benchmark board passed without provider warnings. No API key is
included in this repository.


Validated on 2026-10-23 for Monitor alerts and administration spacing:

- 53 unit tests and all 14 recovered model suites passed
- Monitor uses the local member directory and its own heading; the current
  user and linked-BBS mail hints are excluded
- a monitor event was delivered through the same foreground multiplexer used
  by Chat and IRC, without returning unrelated socket readiness to the caller
- real PTY tests preserved a partially typed line and external-game input
  across a sign-in banner; the alert never became application input
- SOURCE, TYPE and STATUS headings align exactly with source-row values

Monitor subscriptions are per SSH login, as before. Banners use the terminal's
first row with cursor save/restore and a terminal bell. Foreground loops repaint
the notice for 12 seconds; screens retain their own input handling.
