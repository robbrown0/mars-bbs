# Troubleshooting

Permission denied on RPC sockets: inspect ownership and volume initialization against service UIDs. Do not make production databases world-readable.

IRC refuses startup: ensure a new certificate remains valid for over 24 hours, service UID can read the key, and the password is exactly 48 hex characters. Check internal DNS alias and certificate server-name matching.

Chess fails: verify python-chess, `MARS_STOCKFISH=/usr/local/bin/stockfish`, and that `stockfish` reports version 19. A checksum failure during build means the upstream archive differs from the reviewed release and the build should remain stopped.

Information data unavailable: distinguish cached values, remote provider failures and missing optional API credentials.

DOS doors unavailable: the exact historical PPA packages are no longer downloadable. Use an authorized private copy of the preserved deployed image plus legally obtained templates, or validate and document a future emulator upgrade. Never substitute a placeholder and mark it recovered.

External IRC maintenance unavailable: check the ignored JSON configuration, TLS server name, private nickname password, and outbound DNS. The base internal IRC service does not require this optional profile.
