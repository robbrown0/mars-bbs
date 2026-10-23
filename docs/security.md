# Security

Production databases, account records, messages, host keys, registration files, tokens, certificates and raw private recovery artifacts are excluded by repository and build-context ignore rules. Runtime secrets live in `secrets/` as fixed-UID, read-only files. The proposed Git tree was scanned with Gitleaks and manually reviewed for private deployment values.

SSH uses a transport identity named `bbs` with an empty SSH-layer password and a forced command. This identity cannot obtain a shell, execute a remote command, forward ports or create an authenticated BBS session. The forced application flow requires a registered account and Argon2id password before creating a UID-bound session. Guest and anonymous application paths are prohibited. Telnet is absent.

Application services run as fixed non-root UIDs with read-only root filesystems, all Linux capabilities dropped and `no-new-privileges`. The gateway starts as root only to run sshd and hand authenticated users to distinct runtime UIDs; it receives only `AUDIT_WRITE`, `CHOWN`, `SETGID`, `SETUID` and `SYS_CHROOT`. The optional auth initializer receives only the file-ownership capabilities needed for a fresh volume. No service mounts the Docker socket.

MarsAmp is disabled unless its Compose profile is selected. Its music libraries are mounted read-only, its HTTP port binds to loopback, and public access requires an operator-managed HTTPS proxy. A 90-second, single-use pairing ticket creates a secure HTTP-only cookie and a revocable, music-only browser session. That session may outlive a dropped SSH transport while its browser is polling, expires after at most eight hours, and is visible to Sysops under session management. Re-pairing replaces the account's earlier MarsAmp browser session. Password reset, account disablement, account deletion, expiry, and administrative termination revoke it. The browser and terminal APIs expose source labels and opaque track IDs, never filesystem paths or storage credentials. The `Music` role grants playback; catalog rescans and additional-source changes require the separate `music.manage` capability held by a Sysop. Additional sources must already exist below the read-only operator-approved root, which keeps network mounting and credentials out of the application. Host SMB credentials belong in a root-only mount credential file and never in this repository or `.env`.

AirPlay and Cast receivers cannot use the browser's HTTP-only cookie. The
authenticated page therefore receives a per-track signed stream capability
that expires after roughly two hours. Its HMAC binds the opaque track ID, a
version derived from file size/modification state, and expiration. It grants
only byte-range reads of that one unchanged audio file; it cannot read the
catalog, control playback, access another track, or authenticate to the BBS.
The signing key exists only in service memory and changes on restart. Public
proxies should avoid request-path logging for `/marsamp/stream/` because the
path is a temporary bearer capability. Direct Sonos service credentials and
Google Cast SDK credentials are not used or stored.

Core backends use Unix `SOCK_SEQPACKET` sockets and Linux peer credentials on services with `network_mode: none`. Information collection has a separate outbound network. ngIRCd uses a private internal network and publishes no port. The SSH gateway is public by default; the BBS mail-exchange port defaults to a loopback-only host binding and should be exposed only to intended peers.

Linked-BBS mail is text-only and uses a separate non-root exchange process. TLS encrypts it, certificate fingerprints are pinned during administrator pairing, and HMAC-SHA-256 authenticates the timestamped body with a per-peer 256-bit secret. Persisted nonces, stable message IDs, size limits and per-user/per-peer rate limits provide replay, duplicate and basic abuse control. Ordinary users can see peer names, network IDs and delivery state. They cannot retrieve peer endpoints, certificate fingerprints, shared credentials or transport errors. No SMTP, IMAP, POP3 or external mail-client authentication endpoint exists in this phase.

Security checks covered malformed/oversized RPC packets, session-token replay, peer-UID binding, role boundaries, private mail isolation, BBS-link authorization, HMAC and nonce replay checks, duplicate delivery, certificate-pin rejection, path/symlink rejection, terminal-control sanitization, information-source allowlisting, external IRC capability authentication, TLS and SSH forced-command restrictions.

Operators should generate new secrets per installation, keep Docker and base packages updated, bind SSH to loopback during acceptance tests, back up private data consistently, and scan every release commit. Never reuse or publish material from the private recovery snapshot.
