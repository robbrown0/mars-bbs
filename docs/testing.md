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

The subsequent visual-polish release retained all 78 passing tests and valid
generated JavaScript. Production verification confirmed the shortened
**SECURE** badge, inline cast glyph and persistent **SPEAKER CONNECTED** label;
the deployed source hash matched, database integrity remained OK and the new
music container stayed healthy with zero restarts.

The compact-output follow-up retained all 78 passing unit tests and all 14
recovered model suites. Generated JavaScript passed Node syntax validation and
Gitleaks found no secrets. Production verification confirmed that the former
output card is absent, the device glyph shares the play-mode row, the deployed
source SHA-256 is
`3757796637017d02dde2504592c395353ef6e0ed552ec6cc072226836c38d25e`,
the music database remains integral with 26,075 available tracks and 853
curated entries, the public manifest returns HTTP 200 and unauthenticated state
returns HTTP 401. The replacement runs non-root with a read-only root,
`no-new-privileges`, all capabilities dropped, loopback-only HTTP and zero
restarts. The consistent pre-deployment database snapshot SHA-256 is
`ef88a21711de50bb5f65a689abb08894cab09a391e89104d59ba24cc1fe1541a`.

Validated on 2026-10-24 for numbered playlists and browser-player refinement:

- all 79 unit tests and all 14 recovered model suites passed in isolated,
  network-disabled containers; the generated browser JavaScript passed Node
  syntax validation
- the terminal playlist picker accepts zero-padded selections such as `01`
  and `15`, while retaining arrows, Enter and existing transport keys
- the authenticated browser player can switch playlist playback between
  sequential and no-repeat shuffle without restarting the current track;
  full-library playback remains shuffle-only
- the redundant play-mode text, persistent ready notice and instructional
  footer are absent; the accessible shuffle and output controls remain, and
  autoplay failures, progress, authorization expiry and errors still surface
- deployed source SHA-256
  `bfb8f39a1042fa582771e1b6cc2cc6b6e9a4ea7ace4b21207464d1962ee4c7c7`
  matched the tested source
- production remained healthy with zero restarts, database integrity OK,
  26,075 tracks and 853 curated entries; the service remains non-root,
  read-only, capability-free, `no-new-privileges` and loopback-only
- the public manifest returned HTTP 200 and unauthenticated state and
  shuffle requests returned HTTP 401
- the gateway remained running with its original SSH host-key fingerprint
  `SHA256:f46r4okVd7RLfofExvWW5XsM0od7ra4s0eVq6sPon0w`
- the consistent pre-deployment database snapshot SHA-256 is
  `f3cfd67c663f1dc151fc652aab045cee3be8963aa92acd7749f9273d05c03cee`

Validated on 2026-10-24 for quiet shuffle states and playlist-aware queues:

- all 80 unit tests and all 14 recovered model suites passed in isolated,
  network-disabled containers; generated browser JavaScript passed Node syntax
  validation
- the browser toggle reports only `SHUFFLE ON` or `SHUFFLE OFF`; successful
  changes no longer open a redundant status message, while failures and the
  full-library shuffle-only explanation remain visible
- the queue heading shows a curated playlist name only when the server verifies
  that the ordered available track IDs exactly match that playlist; searches
  and custom queues retain the `Current queue` fallback
- the terminal MarsAmp heading no longer displays NAS01/UNR01 priority; both
  storage sources and administrator source management remain unchanged
- deployed `musicd.py` SHA-256 is
  `c4358de71bb194d5a3971c0ae37d5198ab9a06d268dd3789138035076684bc4b`
  and deployed `ui.py` SHA-256 is
  `744bed67eadcd945b8bbe73b336c6964e747354f3acbee554f863406842d9425`
- music and gateway replacements remained healthy/running with zero restarts,
  read-only roots, all capabilities dropped and `no-new-privileges`; the
  public manifest returned HTTP 200 and unauthenticated state/mode requests
  returned HTTP 401
- database integrity remained OK with 26,075 tracks and 853 curated entries;
  the consistent pre-deployment snapshot SHA-256 is
  `ef895f40490fe9f1e57ee3e93b417bb7728fcea3506756533d3a7e4a6690bf87`
- the SSH host-key fingerprint remained
  `SHA256:f46r4okVd7RLfofExvWW5XsM0od7ra4s0eVq6sPon0w`

Validated on 2026-10-24 for aligned browser-player options:

- all 80 unit tests passed in an isolated, network-disabled container
- the Shuffle and Cast controls now use the same five-column grid as the
  transport row, centered directly beneath Stop and Next respectively
- the five-column transport layout is retained on narrow phones so the two
  option controls remain aligned at every supported viewport width
- opening the native AirPlay or Cast picker no longer displays redundant
  instructional text; precondition, unsupported-browser and picker errors
  remain visible
- deployed `musicd.py` SHA-256 is
  `05fb6bdbd5308e86dafa6ee84d482467f5c8655ad2e9b0e2b6721eec1ad47b2e`
- the replacement music service is healthy with zero restarts and retains its
  non-root user, read-only root, dropped capabilities, `no-new-privileges` and
  loopback-only HTTP binding
- database integrity remained OK with 26,075 tracks; the consistent
  pre-deployment snapshot SHA-256 is
  `6520d938fece429705040e9ae7bcb831effa6a724cf9bb08590ecead4cd6bdb1`

Validated on 2026-10-24 for the real browser spectrum analyzer:

- all 80 unit tests passed in an isolated, network-disabled container and the
  generated player JavaScript passed Node syntax validation
- supported browsers capture the playing media element without changing its
  output route, calculate a 512-point FFT and render 18 smoothed frequency
  bands with `AnalyserNode.getByteFrequencyData()`
- the former clock-driven decorative movement is absent; browsers without
  media-element capture display a stationary resting meter
- Safari/WebKit and remote AirPlay/Cast playback deliberately avoid Web Audio
  rerouting so background, lock-screen and native output behavior remain intact
- deployed `musicd.py` SHA-256 is
  `862b095ed5fc938726892b7db1877494109863b8d9151994d30a1bbdc04d8d7b`
- production remained healthy with zero restarts, database integrity OK and
  26,075 tracks; non-root, read-only, dropped-capability,
  `no-new-privileges` and loopback-only restrictions remain in place
- the consistent pre-deployment database snapshot SHA-256 is
  `88515656ef16bc76b1a8511d47c0451fc7202d944dda1140289149ab842b6136`

Validated on 2026-10-24 for iPad spectrum fallback and browser mute:

- all 81 unit tests passed in an isolated, network-disabled container; the
  generated player JavaScript passed Node syntax validation
- Safari/iPhone/iPad and remote-output sessions use an authenticated,
  current-track-only, 18-band timeline synchronized to the audio element's
  playback position; supported browsers retain the local Web Audio FFT path
- fallback generation is limited to one worker, four queued tracks, 30 minutes
  per recording and a rolling 1,000-track cache; recordings remain read-only
  and the response contains neither audio nor paths, credentials or tags
- the browser Mute control is centered beneath Play, with Shuffle beneath Stop
  and Output beneath Next; mute state is accessible and remembered locally
- FFmpeg 6.1.2 successfully produced the expected 18-band raw frames in the
  final image; its Alpine base is digest-pinned and its package manifest is
  preserved in the runtime image
- deployed `musicd.py` SHA-256 is
  `b11d770f7224c2fed34a555a96871f4415c3dc59fffca66dc237eaca076478f4`
- production is healthy with zero restarts, database integrity OK and 26,075
  available tracks; the service remains non-root, read-only,
  capability-free, `no-new-privileges` and bound only to loopback
- unauthenticated state remains HTTP 401; the previous music container is
  retained, stopped, as the immediate rollback target
- the consistent pre-deployment database snapshot SHA-256 is
  `7aef0abc7209e502bc4be518e3c48e0e7a200af5473b08cec816589137b2a856`

Validated on 2026-10-24 for spectrum sensitivity tuning:

- all 81 tests passed in an isolated, network-disabled container
- a 30-second sample from a real read-only NAS track produced 240 analysis
  frames with no decoder errors
- the former direct amplitude mapping averaged only 2–15 percent across most
  bars; the tuned mapping averaged 26–55 percent across the upper 17 bars and
  reached 35–74 percent at the 90th percentile while retaining bass peaks
- 24 logarithmic source buckets are remapped into 18 display bands using
  cube-root amplitude scaling and gradual high-frequency compensation
- analysis versioning invalidates earlier cached timelines without modifying
  source audio files
- uncached tracks publish a 30-second spectrum preview before the full analysis;
  the browser polls at 300 milliseconds until that preview is ready, renders it
  immediately, and refreshes the complete timeline in the background
- on the real NAS sample, the 240-frame preview completed in 0.38 seconds and
  compressed to 3,672 bytes; the complete 1,951-frame track completed in 1.94
  seconds and compressed to 28,231 bytes
- deployed `musicd.py` SHA-256 is
  `966e6dd00cd7a7fecdd730e59180a0a8aee000867e4e9274a14b7365e6d55ea1`
- production remained healthy with zero restarts, database integrity OK and
  26,075 available tracks; non-root, read-only, dropped-capability,
  `no-new-privileges` and loopback-only restrictions remain in place
- the consistent pre-deployment database snapshot SHA-256 is
  `ea3a46c0d98bf1343422b79590334a355753f1e276e7b1a0a4e62f120097c8c2`

Validated on 2026-10-24 for stopped-queue resume and adaptive stream recovery:

- all 82 tests passed in an isolated, network-disabled, read-only container;
  the generated player JavaScript passed Node syntax validation
- Stop retains the selected track, complete queue, remaining randomized
  shuffle bag and history; Play resumes that selection from the browser, and a
  refreshed stopped player preloads the selected stream without autoplaying it
- the expanded queue remains populated after Stop, and successful Play, Pause,
  Stop and Resume commands no longer leave redundant status messages
- the browser requests native adaptive preloading, detects waiting, stalled,
  error and offline/online transitions, preserves the last playback position,
  and retries authenticated range streaming with a bounded exponential backoff
- Save-Data, 2G and sub-1 Mbps browser hints receive a longer recovery grace
  period so a slow buffer is not mistaken for a failed stream
- deployed image is
  `sha256:d722f9d1d4c634c6379c7af2ccd682afe34d2c63a8f92ee14b018335f274a0fd`;
  deployed `musicd.py` SHA-256 is
  `661f4c5c40e50f1373f18ec2f19522a3a7353cbf208d88e9e9065f21f8846bab`
- production is healthy with zero restarts, database integrity OK and 26,075
  available tracks; non-root, read-only, dropped-capability,
  `no-new-privileges` and loopback-only restrictions remain in place
- the prior production container is preserved, stopped, as
  `mars-v2final-music-rollback-pre-resume-buffer-20261024`
- the consistent pre-deployment database snapshot SHA-256 is
  `ca9a668abe4fb11e0580ae51e7286ccc6bac4fb73927fd60e4914f9819312bb8`

Validated on 2026-10-24 for interruption-free Output picker opening:

- all 82 tests passed in an isolated, network-disabled, read-only container;
  the generated player JavaScript passed Node syntax validation
- opening or cancelling the AirPlay/Cast Output picker no longer changes or
  reloads the active audio source
- the short-lived speaker capability stream is activated only after WebKit or
  the W3C Remote Playback API reports an actual remote-device connection; the
  route transition preserves the current playback position
- deployed image is
  `sha256:ff55a105f196480f6adc66a7bcf1dddcdef2ebbb0feedb268f2ba3110ee3dfb8`;
  deployed `musicd.py` SHA-256 is
  `cbb75d4af91d8e9d3f4e753e78d2a0d7664bc2b72c2c55d1b666b3906dcbae29`
- production is healthy with zero restarts, database integrity OK and 26,075
  available tracks; non-root, read-only, dropped-capability,
  `no-new-privileges` and loopback-only restrictions remain in place
- the prior production container is preserved, stopped, as
  `mars-v2final-music-rollback-pre-output-picker-no-dip-20261024`
- the consistent pre-deployment database snapshot SHA-256 is
  `9af12817b6dd5078b0b202f2158619e4c6496ec658a7ff2f519ca9bf9987a9d9`

Validated on 2026-10-24 for one-tap playback with an active AirPlay route:

- all 82 tests passed in an isolated, network-disabled, read-only container;
  the generated player JavaScript passed Node syntax validation
- when a stopped or paused selection is started from the browser, the player
  now invokes `audio.play()` synchronously inside the user's tap before sending
  the Resume request, preserving iOS/WebKit media activation across AirPlay
- the server Resume and immediate browser playback run together; the returned
  state still authoritatively synchronizes the queue and playback state
- an autoplay/AirPlay rejection keeps the highlighted Play prompt visible
  instead of allowing a quiet successful server request to hide it
- deployed image is
  `sha256:a00998c801097873125b59790e0f6d1d0dd33ec8834ae74f531e7c3f799a4ccf`;
  deployed `musicd.py` SHA-256 is
  `59eb73e5d825fbaae06baa3f19ca9c9af022b97271a110a6d8ed2f5ac4d69a9b`
- production is healthy with zero restarts and database integrity OK; the
  startup scan reports 26,178 currently available tracks
- non-root, read-only, dropped-capability, `no-new-privileges` and loopback-only
  restrictions remain in place; unauthenticated state remains HTTP 401
- the prior production container is preserved, stopped, as
  `mars-v2final-music-rollback-pre-airplay-one-tap-20261024`
- the consistent pre-deployment database snapshot SHA-256 is
  `3cda36e47c7e8ee9ab9383f9a9e463cc299cd2060da7d641635f2c79d300c427`

Validated on 2026-10-24 for connected AirPlay Output state:

- all 82 tests passed in an isolated, network-disabled, read-only container;
  the generated player JavaScript passed Node syntax validation
- Output remains enabled whenever WebKit or the Remote Playback API reports an
  active route, even if the same browser reports that no additional targets
  are currently available
- target availability can disable Output only while no remote route is active;
  connected styling, label and accessible state remain synchronized
- deployed image is
  `sha256:70446cfb49c4ae331462d76e6f4a40b29e1f6ebf0756c55373bebb864ed6a245`;
  deployed `musicd.py` SHA-256 is
  `56eb2c761014dca6e59b3052d580395c8767073a6aa16ed2fee02976fffdda73`
- production is healthy with zero restarts, database integrity OK and 26,178
  available tracks; non-root, read-only, dropped-capability,
  `no-new-privileges` and loopback-only restrictions remain in place
- the prior production container is preserved, stopped, as
  `mars-v2final-music-rollback-pre-airplay-output-enabled-20261024`
- the consistent pre-deployment database snapshot SHA-256 is
  `712bc636fb7f1d55f3002b274a7277e30a3ed0e322ce44a99eac53ef4f961f67`

Validated on 2026-10-24 for lock-screen track controls, resilient metadata and
one-tap AirPlay track changes:

- all 82 tests passed in an isolated, network-disabled, read-only container;
  the generated player JavaScript passed Node syntax validation
- Media Session publishes Previous/Next and explicitly removes ten-second
  seek-back/seek-forward handlers while retaining timeline scrubbing
- title, artist and album metadata are committed before artwork, so an artwork
  rejection cannot discard the textual update; Safari/iOS still controls and
  may suppress identifying metadata in Private Browsing
- browser Back/Next and lock-screen Previous/Next pre-arm the active media
  element inside the initiating gesture before synchronizing the replacement
  track with the server, including with an active AirPlay route
- deployed image is
  `sha256:2be5d07bd5fe8328662f0cde0651c3f4603433b97f1ed67c53bef7606a703d7d`;
  deployed `musicd.py` SHA-256 is
  `6141456a185be47861fa8cb1a436bed5c534e665f2cfd0cef05179a2218afae6`
- production is healthy with zero restarts, database integrity OK and 27,486
  available tracks; non-root, read-only, dropped-capability,
  `no-new-privileges` and loopback-only restrictions remain in place;
  unauthenticated state remains HTTP 401
- the prior production container is preserved, stopped, as
  `mars-v2final-music-rollback-pre-lockscreen-controls-20261024`
- the consistent pre-deployment database snapshot SHA-256 is
  `cbc94ccf3c920d824f9a405ffac8eaec3b5a395e6a8d02b0f69f7f36798c9779`

Validated on 2026-10-24 for preauthorized AirPlay track changes:

- all 82 tests passed in an isolated, network-disabled, read-only container;
  the generated player JavaScript passed Node syntax validation
- each authenticated state response carries exact previous/next stream URLs;
  shuffle and dynamic full-library choices are fixed server-side before use,
  and tests verify that the later authoritative transition selects that track
- browser and lock-screen Previous/Next set the already-authorized local or
  remote stream and invoke playback synchronously inside the initiating action,
  before the command request or its response
- navigation capabilities are short-lived and restricted to one unchanged
  track; they disclose no NAS path, account credential, or catalog authority
- deployed image is
  `sha256:e1afe105163e88142a50d2163e57995cab6c3c229a92ec35542cfc9fdda3d9b1`;
  deployed `musicd.py` SHA-256 is
  `df0d57a7a0476da847e84d9651c9eebbbf27f7ff4590152ed4559a0f8d646eac`
- production is healthy with zero restarts, database integrity OK and 27,486
  available tracks; non-root, read-only, dropped-capability,
  `no-new-privileges` and loopback-only restrictions remain in place;
  unauthenticated state remains HTTP 401
- the prior production container is preserved, stopped, as
  `mars-v2final-music-rollback-pre-airplay-preauthorized-skip-20261024`
- the consistent pre-deployment database snapshot SHA-256 is
  `a75da184116f88f84ce56e687394ed94dc54090c55f6a0f6570de1389794479a`

Validated on 2026-10-24 for Safari/AirPlay navigation race recovery:

- all 82 tests passed in an isolated, network-disabled, read-only container;
  the generated player JavaScript passed Node syntax validation
- browser state serials are monotonic, so an older 750 ms poll response cannot
  arrive after Next and restore an earlier track or source
- interrupted/superseded play promises retry as buffering; only Safari's
  explicit `NotAllowedError` displays the one-tap Play request
- temporary AirPlay/Remote Playback disconnect notifications no longer replace
  the active source while the route and next track are being negotiated
- deployed image is
  `sha256:f1f413e59d9fd74757e3b97a6e6eea24d2a505103f30079e4fed4a0ce85ccb51`;
  deployed `musicd.py` SHA-256 is
  `73f7bb213ab2def5fb7a245cc72f6d8f9dee0f4adeb46008d3ab631630e66456`
- production is healthy with zero restarts, database integrity OK and 27,486
  available tracks; non-root, read-only, dropped-capability,
  `no-new-privileges` and loopback-only restrictions remain in place;
  unauthenticated state remains HTTP 401
- the prior production container is preserved, stopped, as
  `mars-v2final-music-rollback-pre-airplay-state-race-20261024`
- the consistent pre-deployment database snapshot SHA-256 is
  `0477511cb485adf8ef67c13b73b66986a8ad13dd9ab33b7f8fca17e95ad68ab4`
