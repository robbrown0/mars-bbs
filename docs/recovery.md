# Recovery record

Recovery began 2026-10-18 on `unr01`. Existing material was never overwritten. A new private archive of prior recovery, reconstructed source and configuration has SHA-256 `e93f62443d32faf307d6c72005868bd5cdf8b82bcefe9c94f3c2d9d46ec2561e`. It is preservation evidence, not a transactionally consistent live-database backup. Raw Docker inventories, inspect records, histories and 12 running-container source captures remain private outside Git.

The canonical package contains 37 Python modules. The selected set matches the later `github-rc-20261017` test image byte-for-byte before the documented portability adaptations. `doord.py` came from the executing door service. Earlier recovered board, chat, door, file, game, mail, session and gateway modules matched their corresponding live copies. Historical variants were catalogued but not merged speculatively. Original hashes appear in `source-provenance.json`; exact deployed image IDs appear in `deployed-images.json`.

## Recovered and validated

- SSH gateway and mandatory BBS authentication
- accounts, Argon2id credentials, UID-bound sessions and administration
- retro terminal UI, ASCII artwork and Pirates/Eagles historical material
- private mail, message boards, Direct Talk, public chat and internal IRC bridge
- file areas and authenticated download flow
- Oregon Trail, WOPR, Maze, Global Thermonuclear War and chess
- information collectors for weather, news, solar/space, amateur-radio-related feeds, earthquakes, ISS/satellites, television and movies
- optional market collector with a separately supplied API key
- Zork, LORD and TradeWars door authority/integration code
- Docker Compose topology, fresh-volume bootstrap, secrets initialization and isolated health checks

Stockfish 19 was recovered exactly: the official release binary hash equals the file in the deployed game container. The build verifies the official archive checksum and carries its license, authors and full source.

## Incomplete or unavailable

- the custom deployed OpenSSH 10.6p1 build inputs were not present; Debian OpenSSH 10.0p1 is the tested compatible replacement
- the historical dosemu2 and FDPP package versions are no longer downloadable from the configured PPA, so `Dockerfile.door` records the build but currently fails at dependency resolution; a private exact image export with SHA-256 `7062bd6d5a52dd2b65e986749f5baea41335ca2cf5490df1ac55a86f4e1278f8` was preserved outside Git
- Zork data, registered LORD files/keys and TradeWars data are private/licensed and excluded; only Zork and LORD were enabled in production, and full interactive door play was not independently reproduced
- market data is unavailable without an operator API key
- the stopped production external-IRC container used maintenance-only mode; recovered native-chat bridge behavior passed local fixtures but was not connected to a real third-party network

## Recovery decisions

Fresh deployment files contain no Unraid paths, external production volumes or fixed production container names. Only SSH is published. All tests used isolated resources. Production application containers were inspected and copied from without stop, restart, replacement or configuration change. No private database, account, message, key or credential is part of the public tree.
