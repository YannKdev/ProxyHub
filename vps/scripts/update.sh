#!/usr/bin/env bash
# Met à jour l'installation : git pull, vérification du .env, reconstruction, attente, smoke test.
# À lancer sur le VPS : ./scripts/update.sh
set -euo pipefail

# Tout est dans une fonction : bash la lit en entier avant que git pull ne modifie ce fichier.
main() {
  cd "$(dirname "$0")/.."

  git pull --ff-only

  # Les valeurs vides de .env.example sont obligatoires.
  local missing=""
  for name in $(grep -oE '^[A-Z_]+=$' .env.example | tr -d '='); do
    grep -qE "^$name=.+" .env || missing="$missing $name"
  done
  if [ -n "$missing" ]; then
    echo "missing in .env:$missing"
    exit 1
  fi

  docker compose up -d --build

  echo "waiting for the controller and for vpn-1 to be up..."
  set -a
  . ./.env
  set +a
  local state=""
  for _ in $(seq 1 40); do
    state=$(curl -s -m 10 -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8090/exits \
      | grep -oE '"state":"[a-z]+"' | head -1 | cut -d'"' -f4 || true)
    [ "$state" = "up" ] && break
    sleep 5
  done
  echo "vpn-1: ${state:-no answer}"

  # Anciennes images du contrôleur laissées par les reconstructions.
  docker image prune -f --filter "label=com.docker.compose.project=proxyhub" >/dev/null

  docker compose ps --format '{{.Name}}  {{.Status}}'
  ./scripts/smoke-test.sh
}

main "$@"
exit
