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

# Sorties à vérifier : id:port. Les sorties facultatives, si leur profil (= leur id) est actif.
exits_to_check="vpn-1:8081"
for entry in cyberghost-1:8082 cyberghost-2:8083; do
  case ",${COMPOSE_PROFILES:-}," in
    *",${entry%:*},"*) exits_to_check="$exits_to_check $entry" ;;
  esac
done

direct_ip=$(curl -fsS --max-time 15 "$ip_service")
echo "direct IP: $direct_ip"

for entry in $exits_to_check; do
  id=${entry%:*}
  port=${entry#*:}
  exit_ip=$(curl -fsS --max-time 30 -x "http://$PROXY_USER:$PROXY_PASSWORD@$host:$port" "$ip_service" || true)
  if [ -n "$exit_ip" ] && [ "$exit_ip" != "$direct_ip" ]; then
    ok "$id exit IP: $exit_ip"
  else
    fail "$id exit IP: '$exit_ip'"
  fi

  for creds in "" "nobody:wrong@"; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -x "http://$creds$host:$port" "http://example.com" || true)
    if [ "$code" = "407" ] || [ "$code" = "401" ]; then
      ok "$id rejects bad credentials ($code)"
    else
      fail "$id accepted bad credentials ($code)"
    fi
  done
done

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$api/health" || true)
[ "$code" = "200" ] && ok "api health" || fail "api health returned $code"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$api/exits" || true)
[ "$code" = "401" ] && ok "api rejects requests without token" || fail "api without token returned $code"

auth="Authorization: Bearer $API_TOKEN"
exits=$(curl -fsS --max-time 30 -H "$auth" "$api/exits" || true)
for entry in $exits_to_check; do
  id=${entry%:*}
  state=$(echo "$exits" | grep -oE "\"id\":\"$id\",\"type\":\"[a-z]+\",\"state\":\"[a-z]+\"" | cut -d'"' -f12 || true)
  [ -n "$state" ] && ok "api lists $id: $state" || fail "api exits: '$exits'"
done

if [ -n "${API_DOMAIN:-}" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$API_DOMAIN/health" || true)
  [ "$code" = "200" ] && ok "api over https ($API_DOMAIN)" || fail "api over https returned $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://$API_DOMAIN/health" || true)
  [ "$code" = "308" ] && ok "http redirects to https" || fail "http returned $code, expected 308"
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
