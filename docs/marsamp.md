# MarsAmp

MarsAmp is an optional authenticated music player for the BBS. The ANSI screen inside the SSH session searches the catalog and sends playback commands. A polished retro HTTPS player supplies the audio because SSH terminal protocols do not provide an audio channel. The same page works in a current desktop browser and in an iPhone or iPad browser. Apple browsers may require the listener to tap **PLAY** once before playback can begin.

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

A Sysop grants the historical-style `Music` role to listeners. Only accounts with `music.play` can search, pair, control playback, or read an audio stream. Pairing creates a single-use URL that expires after 90 seconds. Redeeming it creates an opaque, secure, HTTP-only browser cookie backed by a music-only session. It remains usable when a phone suspends or drops the originating SSH connection, stays alive only while the browser polls, and has an absolute eight-hour limit. A Sysop can see and terminate it under **Administration → Sessions**. Re-pairing replaces the account's previous MarsAmp session. Catalog responses and URLs contain opaque numeric track IDs rather than NAS paths.

When a listener selects a song without an actively polling browser sink, MarsAmp selects the track and immediately presents a boxed OSC 8 **OPEN MARSAMP IN BROWSER** button. Opening it pairs the browser with the already selected song. The player attempts playback immediately where browser policy permits it. If iPhone, iPad, or another browser blocks audible autoplay, the page highlights a large **PLAY** button that must be tapped once. The plain URL and the explicit `L` link-audio command remain available as fallbacks. SSH provides no standard or safe way for a remote application to force a client browser to open.

The player publishes current title, artist, album, position, per-track album artwork, and play/pause/previous/next/seek actions through the browser Media Session API. Compatible phones and desktop systems can therefore present MarsAmp like a dedicated audio service on the lock screen, notification area, headset controls, or system media panel. The operating system ultimately decides which controls and artwork it displays. The page also includes installable web-app metadata and appropriately sized local PNG fallback artwork; these public branding assets contain no account or catalog information.

Album art is resolved in this order: an administrator-confirmed cached cover, artwork embedded in the audio file, a conventional `cover`, `folder`, `front`, or `album` JPEG/PNG/WebP beside the read-only album, then the MarsAmp fallback. Audio files and sidecars are never modified. Track-specific art is issued only after an authenticated browser state request. Because iOS fetches lock-screen artwork from a detached media process that may omit the page cookie, the returned Media Session URL carries a signed, expiring capability restricted to one track image. It cannot authorize audio, account data, BBS access, or another image. NAS paths and provider URLs are not exposed. A bounded memory cache avoids repeated NAS reads.

Under **Administration → MarsAmp → Cover artwork**, a Sysop can find a library track, inspect its current artwork source, and search MusicBrainz for a corrected artist/album match. The BBS presents canonical release-group candidates and requires an explicit `IMPORT` confirmation before retrieving the 1200px front cover from the Cover Art Archive. Confirmed images are stored in the private music database and apply to tracks with that artist/album identity. The service rate-limits MusicBrainz searches, accepts only fixed HTTPS provider hosts and safe image formats, and never lets listeners or administrators supply an arbitrary fetch URL. Set `MARSAMP_ARTWORK_USER_AGENT` to a meaningful application/contact URL when publishing another installation.

Inside MarsAmp, use:

- arrow keys to move, `B` for the previous song, or `N` for the next song
- Enter to play the highlighted song
- `P` to play/pause and `V` to stop
- `S` to switch between sequential and shuffle radio play
- `A` to show the available curated and full-library playlists (`R` remains an alias)
- `F` to find by inferred artist, album, or title
- `L` to create a browser pairing link
- `X` or Escape to return

Leaving MarsAmp for another BBS area preserves the current search or playlist, highlighted song, list position, and sequential/shuffle choice for the rest of that SSH login session. Returning to MarsAmp restores that view and follows the currently playing song if the browser advanced while the listener was elsewhere. The paired browser continues playing in the background and automatically advances through the terminal-selected result list. Sequential mode wraps at the end. Shuffle mode creates a randomized bag of the other songs, plays every song once before refilling it, and never immediately repeats the current song. Browser completion and the terminal `N` command use this same queue; `B` walks back through playback history.

The browser player also provides **BACK** and **FORWARD** buttons. They use the same queue, shuffle bag, and playback history as the terminal controls, so a listener can change songs from the browser while the SSH session is idle or disconnected. An expandable **QUEUE** panel shows the current song and upcoming order without disclosing storage paths. In full-library shuffle it reports the live catalog size and explains that the next track is drawn when playback advances instead of attempting to render thousands of rows.

Twenty built-in playlists are presented with **1980s Essentials** first. Nineteen are curated: the original all-time, classic-rock, 1970s, alternative, country, soul/R&B, songwriter, party, and road-trip collections plus New Wave Neon, Arena Rock Anthems, Alternative Mixtape, Country Roadhouse, Women of Mars, Southern & Roots Rock, Unplugged Sunday, Funk/Disco/Dancefloor, and After Midnight. The expanded 1980s collection contains 148 songs; the nine additional curated lists contain 40–45 each. The current Mars Hotel catalog resolves all 853 curated playlist entries.

The twentieth playlist, **Shuffle Everything**, is dynamic. It draws from every currently available catalog track without copying all filenames or track identifiers into the playlist definition or browser. It always uses shuffle mode, avoids the current and recent selections, and preserves bounded history for **BACK**. The displayed count follows the live catalog after rescans.

The curated selections were evaluated against the [Rolling Stone 500 Greatest Songs](https://au.rollingstone.com/music/music-lists/best-songs-of-all-time-30065/), [Rolling Stone's 200 Best Songs of the 1980s](https://au.rollingstone.com/music/music-lists/200-best-songs-1980s-52935/), the [Library of Congress National Recording Registry](https://www.loc.gov/programs/national-recording-preservation-board/recording-registry/complete-national-recording-registry-listing/), and the recordings actually available on the configured libraries. They are editorial playlists rather than claims of a new numeric rating. The service resolves portable artist/title definitions against the current catalog after every scan. Missing recordings are omitted; it never substitutes a similarly named song or represents an unavailable selection as playable.

The browser streams the original file and does not transcode it. MP3 and M4A make up most of the primary library and have broad browser support. FLAC works in current major browsers. WMA is cataloged but may not play in a browser. Tags are not parsed in this recovered implementation; artist, album, and title are inferred from directory and file names. There is no gapless playback or visual audio analyzer.

Sysops can inspect catalog state, add or remove approved additional locations, and request a rescan through **Administration → MarsAmp**. A rescan skips symbolic links, recycle/trash directories, unsupported extensions, and files that disappear during traversal. Missing sources are reported by source label without disclosing their host paths.

The repository contains no music files, filenames, NAS addresses, SMB credentials, or catalog database. Operators are responsible for copyright and public-performance or streaming rights for every library they expose.


### Compact player on the main menu

Music-enabled accounts see a centered MarsAmp header in the Lobby
when the terminal has room. The artist and title scroll together between
stationary music notes; the box and transport controls stay fixed. No volume
control is shown. Three-line mode puts controls in the bottom border; four-line
mode gives them a separate row. This initial rollout is limited to the main menu.

On the main menu, `B` is Back, `P` is Play/Pause and `N` is Next, immediately.
There is no focus mode or prefix. Main menu entries use `O` for Boards, `E` for
New board messages and `S` for Profile / terminal. Other screens retain their
existing commands. Pausing and resuming preserve the queue, shuffle bag,
history and browser playback position.

The player needs at least 60 columns and 24 rows, or 34 columns and 32 rows.
Smaller terminals keep the existing menu. On a 24-row desktop terminal the
Lobby uses two columns to keep the entire menu and prompt visible.


### Playlist browsing and saved playback

MarsAmp opens with the curated playlist picker on a fresh BBS login. `T` shows
all music; on the track screen `T` toggles between all music and playlists.
`A` also opens the playlist picker, and `F` searches the full catalog. The
picker shows the current or last selected song, with `P` to play/pause it.

The music database saves the selected song, playback queue, sequential/shuffle
mode, shuffle bag and history separately for each account. They survive logout
and music-service restarts. After a restart the selection is restored paused;
`P` resumes it and offers browser pairing if needed. Browser credentials are
not restored. A newly paired browser starts the saved song from its beginning;
pausing and resuming in an already open browser preserves its position.


The administration source table uses the same field widths for headings and
rows, so SOURCE, TYPE and STATUS align with each source's values.


Under **Administration → MarsAmp**, `L` toggles the compact player between
three and four lines. The current mode and the next choice are displayed.
This site-wide setting requires `music.manage` and persists in the music
database across service restarts. Three lines is the default. Each session
adopts a changed layout on its next Lobby redraw so an idle player's border
cannot overwrite the menu below it. Direct B/P/N controls are unchanged.
The Lobby command prompt is labeled `Main Lobby`.
