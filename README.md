# The Mars Hotel BBS

This repository is a source-level recovery of the deployed Mars Hotel BBS. It preserves the Pirates BBS 1.9 / Eagles lineage, terminal interface, artwork, account model and service boundaries found in the October 16–17, 2026 deployment. It does not claim that the lost original Git history was recovered.

Connect to the current public BBS with:

```console
ssh bbs@mars.ee.msstate.edu.pl -p 2223
```

Every visitor reaches a forced BBS command and must authenticate to an application account. Guest and anonymous BBS sessions are not supported. Telnet is not configured.

The recovered implementation includes account and session management, local and linked-BBS private mail, message boards, Direct Talk and public chat, internal IRC and bridge support, file libraries, Oregon Trail, WOPR, Maze, Global Thermonuclear War, chess, information collectors, administrative functions and the retro terminal UI. A Sysop can name the installation and pair it with trusted BBS peers for authenticated text-only mail addressed as `user@bbs-id`. Optional MarsAmp support provides an authenticated ANSI music controller, nineteen curated playlists, dynamic full-library shuffle, and a paired premium browser player with an expandable queue, album artwork and lock-screen media controls for phone and PC clients. Optional door support covers Zork and registered LORD 4.02a. The public LORD archive and the verified public registration are included; Zork story data must be supplied separately. The recovered source contains dormant TradeWars integration residue, but TradeWars was not installed or offered by The Mars Hotel. The Python game named `gtw` is Global Thermonuclear War, not TradeWars.

## Deploy a fresh instance

The default deployment requires Linux x86-64, Docker Engine, Docker Compose v2 and OpenSSL. It uses fresh named volumes and publishes only the SSH gateway.

```console
cp .env.example .env
sudo ./scripts/init-secrets.sh
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

Read `secrets/admin_password` locally for the first `admin` login. Keep the generated secrets directory private and out of backups intended for publication. Change `.env` before startup to select a loopback bind address, port or installation-specific public hostname.

The base deployment is independently buildable without Unraid paths or production data. Optional market credentials, external IRC maintenance and licensed doors are described separately.

## Documentation

- [Architecture](docs/architecture.md)
- [Installation and deployment](docs/deployment.md)
- [Security](docs/security.md)
- [Trusted BBS mail links](docs/federation.md)
- [MarsAmp music service](docs/marsamp.md)
- [Testing](docs/testing.md)
- [Backup and restore](docs/backup-restore.md)
- [Doors and licensed game data](docs/doors.md)
- [Optional services](docs/optional-services.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Licensing and attribution](docs/licensing.md)
- [Recovery report](docs/recovery.md)
- [Pirates/Eagles/current feature comparison](docs/heritage-feature-comparison.md)
- [Source provenance](docs/source-provenance.json)

Recovered first-party files are published for preservation and audit with no blanket relicensing grant. Third-party components retain their own licenses. See the licensing review before redistributing images or adding game data.
