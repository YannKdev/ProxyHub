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
for entry in cyberghost-1:8082 cyberghost-2:8083 cyberghost-3:8084 cyberghost-4:8085; do
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
  state=$(echo "$exits" | grep -oE "\"id\":\"$id\",[^}]*" | grep -oE '"state":"[a-z]+"' | cut -d'"' -f4 || true)
  [ -n "$state" ] && ok "api lists $id: $state" || fail "api exits: '$exits'"
done

if [ -n "${API_DOMAIN:-}" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$API_DOMAIN/health" || true)
  [ "$code" = "200" ] && ok "api over https ($API_DOMAIN)" || fail "api over https returned $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://$API_DOMAIN/health" || true)
  [ "$code" = "308" ] && ok "http redirects to https" || fail "http returned $code, expected 308"
fi

# Proxy d'entrée (port 8080). Avec les deux sorties CyberGhost, les sessions de test restent
# sur CyberGhost : vpn-1 sert encore directement sur 8081 pendant la migration.
entry="$host:8080"
pool=""
case ",${COMPOSE_PROFILES:-}," in
  *,cyberghost-1,*cyberghost-2,* | *,cyberghost-2,*cyberghost-1,*) pool="-pool-cyberghost" ;;
esac
s1="smoke1_$$"
s2="smoke2_$$"
through() { curl -fsS --max-time 60 -x "http://$PROXY_USER$pool-session-$1:$PROXY_PASSWORD@$entry" "$ip_service" || true; }
status_through() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 -x "http://$1@$entry" "http://example.com" || true; }

ip1=$(through "$s1")
ip1_again=$(through "$s1")
if [ -n "$ip1" ] && [ "$ip1" = "$ip1_again" ] && [ "$ip1" != "$direct_ip" ]; then
  ok "entry proxy keeps the IP of a session ($ip1)"
else
  fail "entry proxy session IPs: '$ip1' then '$ip1_again'"
fi
if [ -n "$pool" ]; then
  ip2=$(through "$s2")
  if [ -n "$ip2" ] && [ "$ip2" != "$ip1" ]; then
    ok "entry proxy gives another IP to another session ($ip2)"
  else
    fail "entry proxy second session IP: '$ip2'"
  fi
else
  code=$(status_through "$PROXY_USER-session-$s2:$PROXY_PASSWORD")
  [ "$code" = "503" ] && ok "entry proxy refuses a session without free exit (503)" \
    || fail "entry proxy second session returned $code, expected 503"
fi

# Un seul essai faux : chaque mauvais mot de passe compte pour le blocage de l'IP (5 en 10 min).
for creds in "" "$PROXY_USER:wrong@"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -x "http://$creds$entry" "http://example.com" || true)
  [ "$code" = "407" ] && ok "entry proxy rejects bad credentials (407)" || fail "entry proxy bad credentials returned $code"
done
code=$(status_through "$PROXY_USER-colour-red:$PROXY_PASSWORD")
[ "$code" = "400" ] && ok "entry proxy rejects unknown options (400)" || fail "entry proxy unknown option returned $code"

# Libère les sessions de test (leurs sorties tournent ensuite en tâche de fond).
sessions=$(curl -fsS --max-time 15 -H "$auth" "$api/sessions" || true)
for name in "$s1" "$s2"; do
  id=$(echo "$sessions" | grep -oE "\"id\":\"[a-f0-9]+\",\"label\":\"$name\"" | cut -d'"' -f4 || true)
  [ -n "$id" ] && curl -s -o /dev/null -X DELETE -H "$auth" "$api/sessions/$id"
done

exit $failed
