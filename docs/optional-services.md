# Optional credentials and external services

The default collector runs without private API keys. The market source reported unavailable in isolated testing because no market API credential was provided. To enable it, create secrets/massive_api_key as a private regular file owned by UID 21009, mode 0400, then use docker compose -f compose.yaml -f compose.market.yaml up -d. Never add the key to .env, Compose arguments, Git, or image layers.

compose.external-irc.yaml restores the maintenance-only external nickname adapter represented by the stopped production container. Copy config/external-irc.example.json to the ignored config/external-irc.json, supply your network/server/channel/nickname and enable the adapter. Supply secrets/external_irc_nick_password privately, owned by UID 21008 mode 0400. Then use docker compose -f compose.yaml -f compose.external-irc.yaml --profile external-irc up -d. This profile is optional and has not been tested against the real external network. It does not bridge native Chat; the deployed maintenance service also used --maintenance-only.

The public IRC Center uses PUBLIC_IRC_HOST, PUBLIC_IRC_PORT, PUBLIC_IRC_SERVER_NAME and PUBLIC_IRC_CHANNEL. Defaults preserve the Mars destination. Its internal TLS bridge and external-adapter TLS fixture have passed recovery tests. The native-chat external bridge implementation is recovered and its local capability/presence/relay tests pass; enabling that separate topology requires the documented chatd --bridge-* and external_irc --chat-socket options plus a runtime bridge capability.

Collected content remains attributed to its upstream sources. Provider availability, rate limits, credentials and content licenses remain operator responsibilities. Do not describe unavailable market or authenticated external-network features as operational.


The recovered deployment's market credential was restored on 2026-10-23 using
an owner-only file on persistent appdata storage, mounted read-only into the
information service. Provider authentication and a fresh four-benchmark board
completed successfully without provider warnings. The credential remains
outside the repository; the former RAM-backed root path is not used.
