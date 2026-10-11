#!/usr/bin/env bash
# Vérifie une installation. À lancer sur le VPS, depuis n'importe quel dossier.
# Usage : ./scripts/smoke-test.sh [hôte]   (localhost par défaut)
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
. ./.env
set +a

host="${1:-localhost}"
ip_service="https://api.ipify.org"
api="http://$host:8090"
failed=0

ok() { echo "OK   $1"; }
fail() { echo "FAIL $1"; failed=1; }

direct_ip=$(curl -fsS --max-time 15 "$ip_service")
echo "direct IP: $direct_ip"

vpn_ip=$(curl -fsS --max-time 30 -x "http://$PROXY_USER:$PROXY_PASSWORD@$host:8081" "$ip_service" || true)
if [ -n "$vpn_ip" ] && [ "$vpn_ip" != "$direct_ip" ]; then
  ok "vpn-1 exit IP: $vpn_ip"
else
  fail "vpn-1 exit IP: '$vpn_ip'"
fi

for creds in "" "nobody:wrong@"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -x "http://$creds$host:8081" "http://example.com" || true)
  if [ "$code" = "407" ] || [ "$code" = "401" ]; then
    ok "vpn-1 rejects bad credentials ($code)"
  else
    fail "vpn-1 accepted bad credentials ($code)"
  fi
done

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$api/health" || true)
[ "$code" = "200" ] && ok "api health" || fail "api health returned $code"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$api/exits" || true)
[ "$code" = "401" ] && ok "api rejects requests without token" || fail "api without token returned $code"

auth="Authorization: Bearer $API_TOKEN"
exits=$(curl -fsS --max-time 30 -H "$auth" "$api/exits" || true)
if echo "$exits" | grep -q '"id":"vpn-1"'; then
  ok "api lists vpn-1: $(echo "$exits" | grep -oE '"state":"[a-z]+"' | head -1 || true)"
else
  fail "api exits: '$exits'"
fi

session=$(curl -fsS --max-time 30 -X POST -H "$auth" "$api/exits/vpn-1/sessions" || true)
session_id=$(echo "$session" | grep -oE '"session_id":"[a-f0-9]+"' | cut -d'"' -f4 || true)
if [ -n "$session_id" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "$auth" "$api/sessions/$session_id")
  [ "$code" = "204" ] && ok "api session open and close" || fail "api session close returned $code"
else
  fail "api session open: '$session'"
fi

exit $failed
