# Architecture

SSH gateway forces the bbs transport user into bbs-login. authd authenticates BBS accounts using Argon2id through libsodium. sessiond validates session tokens against Unix peer credentials. A privileged gateway supervisor hands authenticated users into distinct Unix identities. Mail, board, file, chat, game, door, information and IRC bridge services use bounded SOCK_SEQPACKET JSON RPC through separately mounted socket volumes.

The local mail authority retains no network interface. A separate `mail-exchange` process shares only the mail volume, account-lookup socket, peer-key directory and its TLS files. It accepts pinned-TLS, HMAC-authenticated text mail from explicitly paired BBS installations and drains the durable outbound queue. BBS display name, stable network ID, peer routes and delivery state live in the mail database; peer shared secrets are mode-0600 files in the private mail volume.

Service UIDs: auth 21001, session 21002, mail 21003, board 21004, file 21005, chat 21006, IRC bridge 21007, external IRC 21008, information 21009, game 21010, door 21011. Gateway transport UID 29999; IRC UID 10002. Backend processes are non-root. Gateway SSH/supervisor startup requires narrowly scoped capabilities and root.

SQLite stores remain private service volumes. Information, IRC bridge and mail exchange require network connectivity. Other backends have `network_mode: none`. Internal ngIRCd uses a private Docker network with no published ports. The exchange port is bound to loopback by default and is the only application protocol designed for optional peer exposure.
