#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo to set container UID ownership.' >&2; exit 1; }
umask 077
mkdir -p secrets
chmod 0700 secrets
[ -e secrets/admin_password ] || openssl rand -base64 24 > secrets/admin_password
[ -e secrets/irc_server_password ] || openssl rand -hex 24 > secrets/irc_server_password
if [ ! -e secrets/irc_client_password ]; then cp secrets/irc_server_password secrets/irc_client_password; fi
if [ ! -e secrets/irc.key ] && [ ! -e secrets/irc.crt ]; then
 openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 -subj /CN=irc.mars-v2.local -addext subjectAltName=DNS:irc.mars-v2.local -keyout secrets/irc.key -out secrets/irc.crt >/dev/null 2>&1
fi
chown 10002:10002 secrets/irc_server_password secrets/irc.key
chown 21007:21007 secrets/irc_client_password
chmod 0400 secrets/admin_password secrets/irc_server_password secrets/irc_client_password secrets/irc.key
chmod 0444 secrets/irc.crt
echo 'Runtime secrets initialized. Read secrets/admin_password locally for the initial admin login.'
