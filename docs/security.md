# Security

Production databases, account records, messages, host keys, registration files, tokens, certificates and raw private recovery artifacts are excluded by repository and build-context ignore rules. Runtime secrets live in `secrets/` as fixed-UID, read-only files. The proposed Git tree was scanned with Gitleaks and manually reviewed for private deployment values.

SSH uses a transport identity named `bbs` with an empty SSH-layer password and a forced command. This identity cannot obtain a shell, execute a remote command, forward ports or create an authenticated BBS session. The forced application flow requires a registered account and Argon2id password before creating a UID-bound session. Guest and anonymous application paths are prohibited. Telnet is absent.

Application services run as fixed non-root UIDs with read-only root filesystems, all Linux capabilities dropped and `no-new-privileges`. The gateway starts as root only to run sshd and hand authenticated users to distinct runtime UIDs; it receives only `AUDIT_WRITE`, `CHOWN`, `SETGID`, `SETUID` and `SYS_CHROOT`. The optional auth initializer receives only the file-ownership capabilities needed for a fresh volume. No service mounts the Docker socket.

Core backends use Unix `SOCK_SEQPACKET` sockets and Linux peer credentials on services with `network_mode: none`. Information collection has a separate outbound network. ngIRCd uses a private internal network and publishes no port. Only the SSH gateway has a host port. Optional outbound adapters publish no ports.

Security checks covered malformed/oversized RPC packets, session-token replay, peer-UID binding, role boundaries, private mail isolation, path/symlink rejection, terminal-control sanitization, information-source allowlisting, external IRC capability authentication, TLS and SSH forced-command restrictions.

Operators should generate new secrets per installation, keep Docker and base packages updated, bind SSH to loopback during acceptance tests, back up private data consistently, and scan every release commit. Never reuse or publish material from the private recovery snapshot.
