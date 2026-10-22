# MarsAmp

MarsAmp is an optional authenticated music player for the BBS. The ANSI screen inside the SSH session searches the catalog and sends playback commands. A small HTTPS page supplies the audio because SSH terminal protocols do not provide an audio channel. The same page works in a current desktop browser and in an iPhone or iPad browser. Apple browsers may require the listener to tap **PLAY SELECTED SONG** once before playback can begin.

The Mars Hotel deployment uses two read-only libraries in this order:

1. `nas01`, the primary music library
2. `unr01`, the smaller secondary library

The Mac download share is deliberately excluded. It is transient intake storage and must not be mounted into the music service.

MarsAmp is disabled in the base Compose deployment. It is enabled with the `music` profile after the two host paths and an HTTPS reverse-proxy route are ready:

```console
MARSAMP_NAS01_PATH=/srv/music/nas01
MARSAMP_UNR01_PATH=/srv/music/unr01
MARSAMP_EXTRA_ROOT=/srv/music/extra
MARSAMP_PUBLIC_URL=https://bbs.example.net/marsamp
docker compose --profile music config --quiet
docker compose --profile music up -d music
```

Mount both libraries read-only on the Docker host. The service also receives read-only bind mounts, runs as UID/GID 21012, has a read-only root filesystem, drops all capabilities, and has no Docker socket. Do not put SMB credentials in Compose or `.env`; mount remote storage at the host layer with a root-only credential file.

`MARSAMP_EXTRA_ROOT` is a brokered parent directory for sources added later. A host operator first mounts each SMB/NFS library as a read-only child of that directory. A Sysop can then use **Administration → MarsAmp → Add location** and enter a short source name plus that child directory. The service accepts only existing relative directories contained by the approved root. It cannot mount a share, receive storage credentials, or alter files. NAS01 and UNR01 are protected base entries; additional entries can be removed from the catalog without unmounting or deleting their files.

Route the path in `MARSAMP_PUBLIC_URL` to the loopback listener at `MARSAMP_PORT` and preserve byte-range requests. TLS must terminate before the listener is exposed publicly. The service rejects a non-HTTPS public URL except for localhost development. Do not publish port 2444 directly to the Internet.

A Sysop grants the historical-style `Music` role to listeners. Only accounts with `music.play` can search, pair, control playback, or read an audio stream. Pairing creates a single-use URL that expires after 90 seconds. Redeeming it creates an opaque, secure, HTTP-only browser cookie bound to the active BBS account and session. Ending the SSH BBS session also ends browser authorization. Catalog responses and URLs contain opaque numeric track IDs rather than NAS paths.

When a listener selects a song without an actively polling browser sink, MarsAmp selects the track and immediately presents a boxed OSC 8 **OPEN MARSAMP IN BROWSER** button. Opening it pairs the browser with the already selected song. The player attempts playback immediately where browser policy permits it. If iPhone, iPad, or another browser blocks audible autoplay, the page highlights a large **PLAY SELECTED SONG** button that must be tapped once. The plain URL and the explicit `P` pairing command remain available as fallbacks. SSH provides no standard or safe way for a remote application to force a client browser to open.

Inside MarsAmp, use:

- arrow keys to move, or `Z` and `B` for previous and next
- Enter or `X` to play
- `C` to pause and `V` to stop
- `M` to switch between sequential and shuffle radio play
- `S` to search by inferred artist, album, or title
- `P` to create a browser pairing URL
- Escape or `Q` to return

Leaving MarsAmp for another BBS area preserves the current search, highlighted song, list position, and sequential/shuffle choice for the rest of that SSH login session. Returning to MarsAmp restores that view and follows the currently playing song if the browser advanced while the listener was elsewhere. The paired browser continues playing in the background and automatically advances through the terminal-selected result list. Sequential mode wraps at the end; shuffle mode chooses another song from that list.

The browser streams the original file and does not transcode it. MP3 and M4A make up most of the primary library and have broad browser support. FLAC works in current major browsers. WMA is cataloged but may not play in a browser. Tags are not parsed in this first recovered implementation; artist, album, and title are inferred from directory and file names. The player has no queue, shuffle, playlists, gapless playback, visual audio analyzer, or automatic next-track action yet. These limits are displayed here rather than representing planned work as complete.

Sysops can inspect catalog state, add or remove approved additional locations, and request a rescan through **Administration → MarsAmp**. A rescan skips symbolic links, recycle/trash directories, unsupported extensions, and files that disappear during traversal. Missing sources are reported by source label without disclosing their host paths.

The repository contains no music files, filenames, NAS addresses, SMB credentials, or catalog database. Operators are responsible for copyright and public-performance or streaming rights for every library they expose.
