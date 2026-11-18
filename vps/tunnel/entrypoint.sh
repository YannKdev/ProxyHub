#!/bin/sh
set -eu

# Clé d'hôte gardée dans un volume : le téléphone la reconnaît après une reconstruction.
mkdir -p /etc/ssh/keys
if [ ! -f /etc/ssh/keys/ssh_host_ed25519_key ]; then
  ssh-keygen -q -t ed25519 -N "" -f /etc/ssh/keys/ssh_host_ed25519_key
fi

key="${TUNNEL_AUTHORIZED_KEY:-}"
if ! printf '%s' "$key" | grep -qxE 'ssh-ed25519 [A-Za-z0-9+/]+=* ?[A-Za-z0-9@._-]*'; then
  echo "TUNNEL_AUTHORIZED_KEY must be one ed25519 public key (see phone/install.sh)" >&2
  exit 1
fi

# Mêmes limites dans authorized_keys que dans sshd_config, au cas où l'une serait modifiée.
mkdir -p /etc/ssh/authorized_keys
printf 'restrict,port-forwarding,permitlisten="13128",permitlisten="19000" %s\n' "$key" \
  > /etc/ssh/authorized_keys/tunnel
chmod 644 /etc/ssh/authorized_keys/tunnel

ssh-keygen -l -f /etc/ssh/keys/ssh_host_ed25519_key.pub
exec /usr/sbin/sshd -D -e
