#!/usr/bin/env bash
# Met à jour l'installation : git pull, vérification du .env, reconstruction, attente, smoke test.
# À lancer sur le VPS : ./scripts/update.sh
set -euo pipefail

# Tout est dans une fonction : bash la lit en entier avant que git pull ne modifie ce fichier.
main() {
  cd "$(dirname "$0")/.."

  git pull --ff-only

  # Les valeurs vides de .env.example sont obligatoires, celles de CyberGhost si une de ses
  # sorties est active (la deuxième reprend les valeurs de la première).
  local required missing=""
  required=$(grep -oE '^[A-Z_]+=$' .env.example | tr -d '=')
  if grep -qE '^COMPOSE_PROFILES=(.*,)?cyberghost-[0-9]+(,|$)' .env; then
    required="$required CYBERGHOST_USER CYBERGHOST_PASSWORD CYBERGHOST_CERT CYBERGHOST_KEY"
  fi
  for name in $required; do
    grep -qE "^$name=.+" .env || missing="$missing $name"
  done
  if [ -n "$missing" ]; then
    echo "missing in .env:$missing"
    exit 1
  fi

  docker compose up -d --build

  echo "waiting for the controller and for all exits to be up..."
  set -a
  . ./.env
  set +a
  # Une ligne par sortie : "id état".
  local states=""
  for _ in $(seq 1 40); do
    states=$(curl -s -m 10 -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8090/exits \
      | grep -oE '"(id|state)":"[^"]+"' | cut -d'"' -f4 | paste -d' ' - - || true)
    if [ -n "$states" ] && ! echo "$states" | grep -qv ' up$'; then
      break
    fi
    sleep 5
  done
  echo "${states:-controller: no answer}"

  # Anciennes images du contrôleur laissées par les reconstructions.
  docker image prune -f --filter "label=com.docker.compose.project=proxyhub" >/dev/null

  docker compose ps --format '{{.Name}}  {{.Status}}'
  ./scripts/smoke-test.sh
}

main "$@"
exit
