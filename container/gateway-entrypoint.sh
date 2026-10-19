#!/bin/sh
set -eu

install -d -m 0755 /run/sshd
install -d -o 0 -g 0 -m 0700 /run/mars-v2-sshd
install -d -o 0 -g 29999 -m 0770 /run/mars-v2-supervisor
# Search-only for account UIDs: a child can reach its unpredictable 0700
# directory but cannot enumerate sibling session directory names.
install -d -o 0 -g 0 -m 0711 /run/mars-v2-sessions

chown 29999:29999 /run/mars-v2-sshd

# sshd constructs a restricted forced-command environment, so convey the
# configured public Mars endpoint through a root-owned runtime file. The
# transport account can read this value but cannot alter it.
umask 022
printf '%s\n' "${MARS_V2_MARS_ADDRESS:-}" > /run/sshd/mars-address
chmod 0444 /run/sshd/mars-address
printf '%s\n' "${MARS_V2_PUBLIC_HOST:-mars.ee.msstate.edu.pl}" > /run/sshd/mars-hostname
chmod 0444 /run/sshd/mars-hostname

if [ ! -s /var/lib/mars-v2-ssh/ssh_host_ed25519_key ]; then
  ssh-keygen -q -t ed25519 -N '' -f /var/lib/mars-v2-ssh/ssh_host_ed25519_key
  chmod 0600 /var/lib/mars-v2-ssh/ssh_host_ed25519_key
fi

export PYTHONPATH=/opt/mars-v2
/usr/bin/python3 -m marsv2.supervisor \
  --socket /run/mars-v2-supervisor/supervisor.sock \
  --session-socket /run/mars-v2/session/session.sock \
  --session-root /run/mars-v2-sessions &

exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config_v2
