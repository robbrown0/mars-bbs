/bin/bash: warning: setlocale: LC_ALL: cannot change locale (C.UTF-8): No such file or directory
# Recovery record

Recovery began 2026-10-18 on `unr01`. Existing material was never overwritten. A new private archive of prior recovery, reconstructed source and configuration has SHA-256 `e93f62443d32faf307d6c72005868bd5cdf8b82bcefe9c94f3c2d9d46ec2561e`. It is preservation evidence, not a transactionally consistent live-database backup. Raw Docker inventories, inspect records, histories and 12 running-container source captures remain private outside Git.

The recovered baseline contains 37 Python modules. The selected set matches the later `github-rc-20261017` test image byte-for-byte before the documented portability adaptations. `doord.py` came from the executing door service. Earlier recovered board, chat, door, file, game, mail, session and gateway modules matched their corresponding live copies. Historical variants were catalogued but not merged speculatively. Original hashes appear in `source-provenance.json`; exact deployed image IDs appear in `deployed-images.json`.

## Recovered and validated

- SSH gateway and mandatory BBS authentication
- accounts, Argon2id credentials, UID-bound sessions and administration
- retro terminal UI, ASCII artwork and Pirates/Eagles historical material
- private mail, message boards, Direct Talk, public chat and internal IRC bridge
- file areas and authenticated download flow
- Oregon Trail, WOPR, Maze, Global Thermonuclear War and chess
- information collectors for weather, news, solar/space, amateur-radio-related feeds, earthquakes, ISS/satellites, television and movies
- optional market collector with a separately supplied API key
- Zork and registered LORD door operation; dormant TradeWars integration residue is preserved as recovered source only
- Docker Compose topology, fresh-volume bootstrap, secrets initialization and isolated health checks

Stockfish 19 was recovered exactly: the official release binary hash equals the file in the deployed game container. The build verifies the official archive checksum and carries its license, authors and full source.

The exact dosemu2 build installed on October 16 was superseded in the rolling PPA the next day and removed from its APT pool at 00:53 UTC on October 19 (20:53 EDT on October 18). Recovery located Launchpad build record 33849543, checksum-pinned its retained amd64 artifact, and verified that its emulator binary matches production. The exact FDPP, libfdpp and comcom64 dependencies remain pinned as well. A private exact image export with SHA-256 `7062bd6d5a52dd2b65e986749f5baea41335ca2cf5490df1ac55a86f4e1278f8` is also preserved outside Git.

## Incomplete or unavailable

- the custom deployed OpenSSH 10.6p1 build inputs were not present; Debian OpenSSH 10.0p1 is the tested compatible replacement
- Zork data remains excluded and full interactive Zork play was not independently reproduced
- TradeWars was not installed or offered; its dormant integration branch and image-template residue are not presented as an operational Mars Hotel feature
- market data is unavailable without an operator API key
- the stopped production external-IRC container used maintenance-only mode; recovered native-chat bridge behavior passed local fixtures but was not connected to a real third-party network

## Recovery decisions

Fresh deployment files contain no Unraid paths, external production volumes or fixed production container names. Only SSH is published. All tests used isolated resources. Production application containers were inspected and copied from without stop, restart, replacement or configuration change. No private database, account, message or runtime credential is part of the public tree. The included LORD registration is deliberately public by operator attestation and is documented separately from runtime secrets.

## Post-recovery MarsAmp addition

`musicd.py` is new work built after recovery; it is not represented as source recovered from the October images. A read-only inventory on 2026-10-21 found 25,786 supported files in the primary NAS01 music library and 289 in the secondary UNR01 library. The repository includes neither collection. The optional service catalogs both read-only sources, supplies an authenticated SSH control screen and streams original files to a paired phone or PC browser. The Mac intake share was excluded by operator direction.

On 2026-10-22 MarsAmp gained ten curated playlists containing 120 selections resolved from the live catalog. Playback now uses a no-repeat shuffled queue shared by browser completion and terminal controls, and terminal Back follows playback history. The change passed isolated tests and was deployed using newly built music and gateway images after a private recovery snapshot. The previous containers were retained under rollback names.

Later that day the playlists were expanded to 415 verified selections: 35 songs in each general collection and 100 in 1980s Essentials. Every definition was resolved against a copy of the live catalog before deployment; no music or catalog data was added to Git.

On 2026-10-23 the browser sink gained a responsive retro player, local artwork, an expandable path-free queue and Media Session controls for compatible lock screens and system media panels. The 1980s collection was expanded to 148 tracks, nine additional curated playlists were added, and all 853 curated entries resolved against a copy of the 26,075-track production catalog. A twentieth dynamic playlist shuffles the live catalog directly without publishing or persisting a list of NAS files. The exact rebuilt images passed 64 application tests, all 14 recovered model programs, JavaScript syntax validation, copied-database integrity and secret scanning before deployment. The prior music and gateway containers remain stopped under named rollback entries, and the SSH host key was unchanged.

Later on 2026-10-23 MarsAmp added per-track artwork to the browser player and Media Session metadata. It automatically prefers embedded artwork, then conventional album-folder images, and otherwise uses the MarsAmp graphic. Administrators can search MusicBrainz, review an exact release-group match, and explicitly import a 1200px front cover from the Cover Art Archive into the private music database; user browsers never contact either provider. Confirmed covers are authenticated, path-free, size-bounded and excluded from Git. The exact deployment passed 69 application tests, the recovered model suite, JavaScript and Compose validation, a migration rehearsal against a read-only production backup, live local-art extraction and secret scanning. Production retained 26,075 tracks, 20 playlists and 853 curated entries, and the previous music and gateway containers remain available for rollback.

An iPhone production check then showed that Safari displayed the cover inside the authenticated MarsAmp page but its detached lock-screen media process omitted the session cookie and received no artwork. MarsAmp now issues the authenticated state response a signed, expiring capability restricted to the current track image and artwork version. It grants no audio, catalog, account, control or BBS access; tampering, cross-track reuse and expiration are rejected. The exact replacement music image passed all 69 application tests, the recovered model suite, JavaScript validation and secret scanning. Production database counts, container hardening and the SSH host key remained unchanged, and the prior music container was retained for rollback.

A subsequent normal-Safari check confirmed lock-screen artwork. MarsAmp now supplies WebKit self-contained image data, republishes the current track metadata at bounded audio lifecycle points, presents the iOS secondary line as `Artist · Album`, and hides filename-derived track/disc prefixes without changing catalog or NAS data. A playlist-first audit then checked all 853 curated entries and all 358 unique selected tracks against an isolated production-database backup and read-only music mounts. The deployed selection contains 313 tracks with embedded art, 29 with folder art and 16 covered by 12 administrator-reviewed album overrides; no curated track lacks artwork. The audit also corrected verbose filename matching so “Master of Puppets” selects the title track rather than “Battery” and moved all curated “Stayin' Alive” entries to the existing `Number Ones` copy with embedded art. The exact image passed 71 application tests, JavaScript validation and a second live audit; database integrity, container hardening and the SSH host key remained unchanged.

The 2026-10-23 metadata campaign then read embedded tags from all 26,075 NAS01 and UNR01 tracks without modifying media files. MarsAmp now keeps raw path-derived values separately, uses file-version-bound canonical title/artist/album metadata with provenance and confidence, rejects placeholder tags, and gives Sysops an audited review/reset screen. The private audit found 25,462 complete tag sets; production accepted 25,835 usable canonical rows and retained conservative fallbacks for 240 tracks. A deployment check caught canonical artist credits changing curated playlist resolution; the final resolver therefore keeps the exact audited raw identity for selection while using canonical names only for presentation. The final production database is integral and retains 20 playlists, all 853 entries, the exact 358 selected track IDs, and all 12 confirmed artwork identities. The release passed 77 application tests, all 14 recovered model suites, a real-file tag test, copied-production migration rehearsals and secret scanning. Music and gateway containers remain read-only with all capabilities dropped and zero restarts; prior images and two private database snapshots remain available for rollback. The SSH host fingerprint remains `SHA256:f46r4okVd7RLfofExvWW5XsM0od7ra4s0eVq6sPon0w`.

Later on 2026-10-23 the MarsAmp webpage gained a native **AIRPLAY / CAST**
output picker. Safari uses Apple's playback-target picker and compatible
browsers use the W3C Remote Playback API. Detached receivers receive a
short-lived, single-track HMAC capability rather than the browser cookie;
tampering, cross-track reuse and expiration are rejected. AirPlay-capable
Sonos products work through the Apple picker. A direct Sonos content-service
integration remains outside this release because it requires Sonos partner
registration, authentication, API implementation and validation. The release
passed 78 application tests, all 14 recovered model suites, generated-player
JavaScript validation, a copied-production database rehearsal and secret
scanning. Production remains at 26,075 tracks, 25,835 canonical metadata rows,
20 playlists and 853 curated entries. Only the music container changed; it is
healthy, read-only, non-root, capability-free and bound to loopback, and its
immediate predecessor plus a consistent private database snapshot were
retained for rollback.

The same evening, the player header badge was shortened to **SECURE** and the
remote-output control received a scalable cast-device glyph, refined spacing
and a persistent connected treatment. The rendering change passed all 78
application tests, generated-player JavaScript validation and secret scanning.
The deployed source hash matched the tested source; the replacement music
container is healthy with zero restarts, and its predecessor plus a fresh
consistent database snapshot remain available for rollback.

The remote-output control was then folded into the ordinary player chrome: the
standalone explanatory card was removed, a compact industry-familiar device
glyph now shares the play-mode row, and phones show the glyph without redundant
copy. Hover, keyboard focus and a restrained green connected indicator preserve
clear state without competing with playback controls. All 78 application tests,
all 14 recovered model suites, generated-player JavaScript validation and
secret scanning passed. Only the music container was replaced; its predecessor
and a consistent private database snapshot remain available for rollback.
