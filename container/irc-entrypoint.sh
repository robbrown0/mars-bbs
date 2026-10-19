#!/bin/sh
set -eu

case ${IRC_ALLOW_PEERING:-no} in
  no) passive=--passive ;;
  yes) passive= ;;
  *) echo "IRC_ALLOW_PEERING must be yes or no" >&2; exit 64 ;;
esac

if [ ! -r /run/secrets/irc.crt ] || [ ! -r /run/secrets/irc.key ] || \
   [ ! -r /run/secrets/irc_password ]; then
  echo "IRC TLS certificate, key, and server password are required" >&2
  exit 78
fi
if ! openssl x509 -in /run/secrets/irc.crt -noout -checkend 86400 >/dev/null; then
  echo "IRC TLS certificate is invalid or expires within 24 hours" >&2
  exit 78
fi
irc_password=$(sed -n '1p' /run/secrets/irc_password)
case "$irc_password" in
  *[!0-9a-fA-F]*|'') echo "IRC server password has an invalid format" >&2; exit 78 ;;
esac
if [ "${#irc_password}" -ne 48 ]; then
  echo "IRC server password must be 48 hexadecimal characters" >&2
  exit 78
fi
runtime_config=/run/mars-irc/ngircd.conf
sed "/^Network =/a Password = $irc_password" /opt/mars-irc/ngircd.conf > "$runtime_config"
chmod 0600 "$runtime_config"
unset irc_password
if ! ngircd --config "$runtime_config" --configtest >/dev/null; then
  echo "ngIRCd configuration validation failed" >&2
  exit 78
fi

echo "Mars Hotel Revival IRC starting; TLS 6697 published, plaintext 6667 internal only" >&2
if [ -n "$passive" ]; then
  echo "IRC server peering is disabled (passive mode)" >&2
  exec /usr/bin/tini -g -- ngircd --nodaemon --passive --config "$runtime_config"
fi
echo "IRC server peering explicitly enabled" >&2
exec /usr/bin/tini -g -- ngircd --nodaemon --config "$runtime_config"
