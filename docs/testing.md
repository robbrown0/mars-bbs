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

Validated on 2026-10-23 for the MarsAmp metadata normalization campaign:

- all 77 unit tests and all 14 recovered model suites passed in isolated,
  network-disabled test containers
- a read-only audit parsed tags from all 26,075 production tracks with zero
  read errors; its private path inventory remains outside Git
- an isolated real-file fixture returned `Sweet Child O' Mine`,
  `Guns N' Roses`, and `Greatest Hits` from embedded tags while leaving the
  source file untouched
- canonical metadata invalidation, placeholder rejection, administrator
  confirmation/audit/reset, raw-value retention and legacy cover lookup passed
- curated playlists contained no placeholder metadata; the three untagged
  playlist tracks retained clean path-derived names
- a production-database rehearsal preserved all 853 curated entries and the
  exact prior set of 358 selected track IDs after canonicalization


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


Validated on 2026-10-23 for compose-screen flicker:

- 56 unit tests passed, plus the recovered editor/key-decoder model
- a real PTY produced no output during an idle compose interval spanning
  multiple resize-poll ticks
- typing updated changed rows without clearing the screen or repainting the
  heading; cursor-only movement did not erase or repaint text rows
- resizing retained the message and refreshed the frame for the new dimensions
- a Monitor banner preserved the composed message and the send confirmation

The shared mail/board editor keeps its periodic resize check, but redraws only
when its state changes. Changed content rows are updated in place. A complete
clear is used only for the initial frame and a terminal resize.


Validated on 2026-10-23 for the saved compact-player layout toggle:

- 61 unit tests and all 14 recovered model suites passed
- three- and four-line players retained aligned borders and B/P/N controls
  across narrow and wide terminals, both playing and paused
- listeners could read the setting but could not change it; only music.manage
  could select 3 or 4 lines, and other values were rejected
- the chosen layout survived reopening the music database
- the administration L command changed the saved setting and refreshed the
  local status cache; periodic title updates retained the current frame height
  until the next Lobby redraw
- the compact player's scroll region followed its actual number of rows

The new player defaults to three lines, with the transport keys in its bottom
border. Four-line mode keeps a separate transport row. The main prompt is now
`Main Lobby`. Both changes are included with the layout setting.


Validated on 2026-10-23 for MarsAmp remote output:

- all 78 unit tests and all 14 recovered model suites passed in isolated,
  network-disabled containers; generated player JavaScript passed Node syntax
  validation and Gitleaks found no secrets

- AirPlay markup, the native WebKit playback-target picker and the W3C Remote
  Playback picker are present without loading a third-party JavaScript SDK
- signed stream URLs contain only opaque IDs, versions, expiry and HMAC values;
  browser cookies, filesystem paths and credentials are absent
- anonymous cookie-authenticated audio remains denied while a valid receiver
  capability supports byte ranges; tampering and expiration return HTTP 403
- automatic track advance selects a fresh receiver URL while remote playback
  is connected, preserving the queue and shuffle behavior
- unsupported browsers keep ordinary local playback and show a system-output
  fallback message rather than exposing or weakening the stream
- a copied-production rehearsal retained database integrity, 26,075 tracks,
  20 playlists and all 853 curated entries
- the deployed source hash matched the tested source; the replacement stayed
  healthy with zero restarts, a non-root identity, read-only root filesystem,
  all capabilities dropped and only the existing loopback HTTP binding
- the public manifest returned HTTP 200, unauthenticated state remained HTTP
  401 and a forged receiver capability returned HTTP 403
