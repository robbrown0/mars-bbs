# Backup and restore

Backups contain account data and private messages. Encrypt them at rest, restrict access and never commit them. Include the named data volumes, file-library content, SSH host-key volume and runtime configuration. Back up `secrets/` separately under tighter access control. The mail volume includes the BBS identity, queues and mode-0600 peer link credentials; preserve it consistently with `secrets/federation.crt` and `secrets/federation.key` or plan to re-pair every linked BBS.

SQLite databases must be copied with the SQLite online backup API or during a controlled service stop that also accounts for WAL files. Copying an active database file alone is not a valid backup. Docker volume archives should preserve numeric owners documented in [architecture.md](architecture.md).

Restore into a new isolated Compose project first:

1. create fresh named volumes without starting public services
2. restore each database/content archive to its matching volume and numeric owner
3. run `PRAGMA integrity_check` on each SQLite database
4. start backends on `network_mode: none`, allow migrations, and inspect health
5. bind the gateway to loopback and test authentication, private mail isolation, boards, files and games
6. test each restored BBS mail link from loopback or an isolated peer network
7. expose the public SSH and any deliberately enabled federation port only after validation

Generate new SSH, IRC and federation keys for a new installation unless continuity of an existing trusted identity is explicitly required. Replacing the federation certificate changes its pin and requires new invitations at every peer. Never restore production credentials into a test stack. The recovery snapshot described in `recovery.md` preserves prior artifacts but is not represented as a transactionally consistent current backup.
