# Troubleshooting

Permission denied on RPC sockets: inspect ownership and volume initialization against service UIDs. Do not make production databases world-readable.

IRC refuses startup: ensure a new certificate remains valid for over 24 hours, service UID can read the key, and the password is exactly 48 hex characters. Check internal DNS alias and certificate server-name matching.

Chess fails: verify python-chess, `MARS_STOCKFISH=/usr/local/bin/stockfish`, and that `stockfish` reports version 19. A checksum failure during build means the upstream archive differs from the reviewed release and the build should remain stopped.

Information data unavailable: distinguish cached values, remote provider failures and missing optional API credentials.

DOS doors unavailable: verify that Launchpad build record 33849543 and the dosemu2 PPA are reachable, then confirm Docker reports the expected checksum for the retained dosemu2 artifact. The complete deployed image is also preserved privately for authorized recovery. Supply legally obtained door templates separately; never substitute a placeholder and mark it recovered.

External IRC maintenance unavailable: check the ignored JSON configuration, TLS server name, private nickname password, and outbound DNS. The base internal IRC service does not require this optional profile.

MarsAmp lock-screen information missing on iPhone or iPad: first confirm playback
from the authenticated browser page. Safari Private Browsing has been observed
to omit the title, artist, album or artwork from the iOS/iPadOS Now Playing
surface even while audio continues. MarsAmp already publishes standard Media
Session metadata and inline artwork, but a webpage cannot force Safari to
expose private-tab metadata to the operating system. Use a normal Safari tab
for reliable lock-screen information. Reopen the one-time BBS link after a
music-service restart or when the browser authorization expires.
