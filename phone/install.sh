#!/data/data/com.termux/files/usr/bin/bash
# Installe la sortie mobile dans Termux : proxy, agent, tunnel vers le VPS, démarrage au boot.
# Relançable sans effet de bord.
# Usage : bash phone/install.sh VPS [port SSH du tunnel, 2222 par défaut]
set -euo pipefail

vps=${1:?usage: bash phone/install.sh VPS [ssh port]}
ssh_port=${2:-2222}
here=$(cd "$(dirname "$0")" && pwd)
conf=$HOME/.config/proxyhub
services=$PREFIX/var/service
logs=$PREFIX/var/log/sv

pkg install -y tinyproxy openssh python termux-services

mkdir -p "$conf" "$HOME/.termux/boot"
chmod 700 "$conf"
if [ ! -f "$conf/id_ed25519" ]; then
  ssh-keygen -q -t ed25519 -N "" -C proxyhub-phone -f "$conf/id_ed25519"
fi
printf 'PROXYHUB_VPS=%s\nPROXYHUB_SSH_PORT=%s\n' "$vps" "$ssh_port" > "$conf/env"

# Un service runit par processus, relancé dès qu'il s'arrête. Le script run est lu sur l'entrée
# standard. Journaux : $PREFIX/var/log/sv/<nom>/current
service() {
  local name=$1
  mkdir -p "$services/$name/log" "$logs/$name"
  {
    echo "#!$PREFIX/bin/sh"
    echo "set -a; . $conf/env; set +a"
    cat
  } > "$services/$name/run"
  printf '#!%s/bin/sh\nexec svlogd -tt %s\n' "$PREFIX" "$logs/$name" > "$services/$name/log/run"
  chmod 755 "$services/$name/run" "$services/$name/log/run"
}

service proxyhub-proxy <<EOF
exec tinyproxy -d -c $here/tinyproxy.conf 2>&1
EOF

service proxyhub-agent <<EOF
exec python -u $here/agent/agent.py 2>&1
EOF

# Pause avant chaque connexion : 4G coupée, ou VPS qui garde encore l'ancienne session (30 s).
service proxyhub-tunnel <<EOF
sleep 5
exec ssh -N -i $conf/id_ed25519 \\
  -o BatchMode=yes -o ExitOnForwardFailure=yes -o ConnectTimeout=20 \\
  -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \\
  -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$conf/known_hosts \\
  -R 13128:127.0.0.1:8888 -R 19000:127.0.0.1:9000 \\
  -p "\$PROXYHUB_SSH_PORT" "tunnel@\$PROXYHUB_VPS" 2>&1
EOF

# Au démarrage du téléphone (application Termux:Boot) : verrou de veille, puis les services.
cat > "$HOME/.termux/boot/proxyhub" <<EOF
#!$PREFIX/bin/sh
termux-wake-lock
. $PREFIX/etc/profile.d/start-services.sh
EOF
chmod 755 "$HOME/.termux/boot/proxyhub"

termux-wake-lock
. "$PREFIX/etc/profile.d/start-services.sh"
export SVDIR=$services
for _ in $(seq 1 20); do
  sv status proxyhub-proxy proxyhub-agent proxyhub-tunnel >/dev/null 2>&1 && break
  sleep 1
done
sv restart proxyhub-proxy proxyhub-agent proxyhub-tunnel || true

# Magisk 31 et plus refuse sans rien demander tant que Termux n'est pas autorisé dans l'appli.
if [ "$(su -c 'id -u' 2>/dev/null)" = "0" ]; then
  echo "root: ok"
else
  echo "root: NOT available (Magisk > Superuser > + > Termux; older Magisk: answer the prompt)"
fi

echo
echo "line to add to vps/.env:"
echo "TUNNEL_AUTHORIZED_KEY=\"$(cat "$conf/id_ed25519.pub")\""
