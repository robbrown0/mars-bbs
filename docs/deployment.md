# Installation and deployment

## Requirements

- Linux x86-64 with Docker Engine and the Docker Compose v2 plugin or standalone binary
- OpenSSL for first-run secret generation
- outbound HTTPS during image builds and for information collectors
- a DNS name and TCP port for public SSH, if the instance will be public
- an HTTPS origin and restricted TCP route to port 2443 if BBS mail linking is enabled
- two read-only host music paths and an HTTPS reverse-proxy path if MarsAmp is enabled

No Unraid paths or existing Mars Hotel volumes are required. Exact base-image digests, Python dependencies and the Stockfish 19 archive checksum are pinned in the Dockerfiles. Distribution package repositories remain an external build dependency.

## First start

```console
git clone https://github.com/robbrown0/mars-bbs.git
cd mars-bbs
cp .env.example .env
sudo ./scripts/init-secrets.sh
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

`init-secrets.sh` creates an initial administrator password, separate IRC server/client password files, a private one-year IRC certificate and a private federation certificate. It never overwrites existing files. Root is required only so files can be owned by the fixed non-root service UIDs. The one-shot `auth-init` service creates the administrator only when the account store is empty.

Read `secrets/admin_password` locally and log in as `admin`. Protect or rotate that credential according to local policy. A normal installation exposes SSH at `${BBS_BIND_ADDRESS}:${BBS_PORT}` and a loopback-only mail exchange at `${BBS_FEDERATION_BIND_ADDRESS}:${BBS_FEDERATION_PORT}`.

## Configuration variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMPOSE_PROJECT_NAME` | `mars-hotel` | Prefix for isolated Docker resources |
| `BBS_BIND_ADDRESS` | `0.0.0.0` | Host address for the SSH gateway |
| `BBS_PORT` | `2223` | Host SSH port |
| `BBS_PUBLIC_HOST` | `mars.ee.msstate.edu.pl` | Hostname shown by the BBS |
| `BBS_PUBLIC_IPV4` | empty | Optional displayed IPv4 address |
| `BBS_DOWNLOAD_HOST` | Mars public host | Download connection hostname |
| `BBS_FEDERATION_BIND_ADDRESS` | `127.0.0.1` | Host address for the trusted BBS mail-exchange listener |
| `BBS_FEDERATION_PORT` | `2443` | Host port for the trusted BBS mail-exchange listener |
| `MARSAMP_NAS01_PATH` | `/srv/music/nas01` | Primary read-only music library on the host |
| `MARSAMP_UNR01_PATH` | `/srv/music/unr01` | Secondary read-only music library on the host |
| `MARSAMP_EXTRA_ROOT` | `/srv/music/extra` | Approved root containing optional additional read-only libraries |
| `MARSAMP_SPECTRUM_CHANNEL_PATH` | `/srv/music/spectrum-channel` | Optional read-only directory containing a generated `spectrum.json` channel playlist |
| `MARSAMP_PUBLIC_URL` | example HTTPS URL | Public browser pairing and playback path |
| `MARSAMP_PORT` | `2444` | Loopback-only MarsAmp HTTP listener |
| `MARSAMP_ARTWORK_USER_AGENT` | Mars Hotel project URL | Meaningful application/contact identity for administrator-triggered MusicBrainz searches |
| `MARS_INFORMATION_USER_AGENT` | generic Mars identifier | Collector HTTP User-Agent; add an operator contact if desired |
| `PUBLIC_IRC_*` | Mars IRC defaults | Destination shown in IRC Center |
| `MARS_DOOR_IMAGE` | local reconstructed door image | Optional preloaded compatible door image |
| `MARS_ENABLED_DOORS` | `zork,lord` | Optional enabled door IDs |
| `MARS_LORD_NODES` | `4` | LORD node count |

Do not put passwords, tokens or private keys in `.env`. Optional credentials use read-only files described in [optional-services.md](optional-services.md).

The loopback exchange default is suitable behind a TLS-aware TCP proxy or private forwarding layer. Direct peer access requires an intentional bind-address and firewall change. Pair and name installations through **Administration → Federation** as described in [federation.md](federation.md). That workflow does not enable Internet email or external mail-client access.

MarsAmp is an optional Compose profile and is not started by the base command. Follow [marsamp.md](marsamp.md) to supply two read-only music libraries and an HTTPS path. Do not mount transient download or intake directories into the service.

## Operations

```console
docker compose ps
docker compose logs --tail=100 gateway auth session
docker compose build --pull
docker compose up -d
```

Review a new image in an isolated Compose project before upgrading a public installation. Database migrations occur in the recovered services and were tested against fixture copies; take private, consistent backups before any upgrade.

Use `127.0.0.1` for `BBS_BIND_ADDRESS` during local validation. Production cutover, DNS changes and importing recovered production data are separate operator actions and are not performed by this repository.
