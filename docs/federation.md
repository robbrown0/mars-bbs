# Trusted BBS mail links

Mars Hotel installations can exchange private, text-only mail without exposing account credentials or a general-purpose mail server. A member addresses a linked installation as `userid@bbs-id`. Local mail continues to use `userid`.

This is an application-level BBS exchange. It does not provide SMTP, IMAP, POP3, Internet email addresses, attachments, HTML mail or external-client login. Those functions are deliberately outside this phase.

## Identity

A Sysop opens **Administration → Federation → Name and identity** and sets:

- **BBS display name:** the human-readable name shown in the lobby and peer directory, such as `The Mars Hotel`.
- **Stable network ID:** 2–32 lowercase letters, numbers or hyphens, such as `mars-hotel`. This becomes the address suffix. It cannot be changed after links or exchange history exist.
- **Public HTTPS exchange origin:** the address another installation uses to reach TCP 2443, such as `https://bbs.example.net:2443`. A reverse proxy may supply the public origin while Compose remains bound to loopback.

The display name can be changed later without changing mail addresses.

## Pair two installations

1. Generate runtime secrets on both hosts with `sudo ./scripts/init-secrets.sh` and start both deployments.
2. Configure each BBS name, stable network ID and public HTTPS exchange origin.
3. On installation A, choose **Create first invitation**. Transfer the resulting `MHX1` code to B's Sysop through a private, authenticated channel.
4. On B, choose **Import invitation**. B can now address users on A.
5. On B, open **Trusted peers**, select A and choose **Reciprocal code**. Transfer that code privately to A.
6. Import the reciprocal code on A. Both directions are now configured.
7. Test with a normal BBS account by composing mail to `userid@remote-bbs-id`.

An invitation contains an endpoint, certificate fingerprint and 256-bit shared link credential. It is administrator-only pairing material. The service shows it only during creation or an explicit reciprocal-export action. Importing it writes the credential to `mail_data/peer-secrets/<bbs-id>.key` with mode `0600`; ordinary-user RPC responses never contain credentials, endpoints or certificate details.

Use a new invitation to rotate a link. Importing over an existing peer requires the explicit `ROTATE` confirmation. Either Sysop may disable a link immediately. Unlinking is blocked while outbound messages remain queued.

## Network exposure

Compose publishes the exchange on `127.0.0.1:2443` by default:

```dotenv
BBS_FEDERATION_BIND_ADDRESS=127.0.0.1
BBS_FEDERATION_PORT=2443
```

For direct host-to-host exchange, bind it to an appropriate host address and restrict TCP 2443 at the firewall to known peers. A TLS reverse proxy or private overlay network can instead forward to the loopback listener. The public origin entered in Administration must match the address peers can reach.

The generated self-signed certificate is acceptable because pairing pins its exact SHA-256 fingerprint. A publicly trusted certificate can replace `secrets/federation.crt` and `secrets/federation.key`; repeat the invitation exchange whenever that certificate changes.

## Delivery and abuse controls

- TLS 1.2 or newer encrypts every exchange connection.
- Each request is authenticated with HMAC-SHA-256 over its timestamp, nonce and complete JSON body.
- Certificate pinning rejects an unexpected endpoint certificate before message data is sent.
- Five-minute timestamp windows and persisted nonces reject replays.
- Stable message IDs make retry delivery idempotent.
- Subjects are at most 72 characters and bodies are at most 8,192 text characters. Terminal controls and binary/MIME payloads are rejected.
- A member can queue at most 20 remote messages per rolling hour.
- A peer can deliver at most 120 messages per rolling hour.
- Transient failures retry with bounded exponential backoff. Permanent recipient or authentication failures are marked failed.
- Users see peer names, network IDs and `QUEUED`, `RETRY`, `DELIVERED` or `FAILED` state. They do not receive the peer endpoint, certificate fingerprint, shared credential or transport error detail.

The network-facing `mail-exchange` process is separate from the session-facing mail authority. The mail authority retains `network_mode: none`. The exchange process runs as UID 21003 with a read-only root filesystem, all capabilities dropped and access only to the mail volume, the account lookup socket, its TLS files and the federation network.

## Operations

The administrator peer list reports enabled state and queued/failed counts. Federation changes appear in the administration audit log without recording invitation codes or shared credentials.

Back up the mail volume and `secrets/federation.crt`/`secrets/federation.key` together. The mail volume contains peer credentials as well as messages and queues. Restoring only part of that set will break certificate pins or link authentication.

No inbound production firewall change or peer trust relationship is created automatically by this repository. Configure and test each link deliberately before advertising its network ID.
