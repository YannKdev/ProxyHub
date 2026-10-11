#!/usr/bin/env bash
# Vérifie les sorties d'une installation. À lancer sur le VPS, depuis n'importe quel dossier.
# Usage : ./scripts/smoke-test.sh [hôte]   (localhost par défaut)
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
. ./.env
set +a

host="${1:-localhost}"
ip_service="https://api.ipify.org"
failed=0

direct_ip=$(curl -fsS --max-time 15 "$ip_service")
echo "direct IP: $direct_ip"

vpn_ip=$(curl -fsS --max-time 30 -x "http://$PROXY_USER:$PROXY_PASSWORD@$host:8081" "$ip_service" || true)
if [ -n "$vpn_ip" ] && [ "$vpn_ip" != "$direct_ip" ]; then
  echo "OK   vpn-1 exit IP: $vpn_ip"
else
  echo "FAIL vpn-1 exit IP: '$vpn_ip'"
  failed=1
fi

for creds in "" "nobody:wrong@"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -x "http://$creds$host:8081" "http://example.com" || true)
  if [ "$code" = "407" ] || [ "$code" = "401" ]; then
    echo "OK   vpn-1 rejects bad credentials ($code)"
  else
    echo "FAIL vpn-1 accepted bad credentials ($code)"
    failed=1
  fi
done

exit $failed
