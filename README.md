# proxyhub

Passerelle proxy HTTP hébergée sur un VPS, pour faire sortir des scripts par une IP choisie. Le client utilise le VPS comme un proxy classique, et le port choisit la sortie : IP mobile 4G ou IP NordVPN (ou autre). Une API permet de changer d'IP (ou de pays pour le VPN) à la demande et garde l'historique des IP.

Le téléphone est derrière le NAT de l'opérateur : c'est le Raspberry Pi qui ouvre un tunnel vers le VPS, jamais l'inverse.

```text
scripts ──> VPS:8080 ──> tunnel SSH ──> Raspberry Pi ──> téléphone 4G    (à venir)
            VPS:8081 ──> Gluetun ──> NordVPN
            VPS:8090 ──> API : état, IP, rotation
```

## Installation (VPS)

```sh
git clone https://github.com/YannKdev/ProxyHub.git proxyhub
cd proxyhub/vps
cp .env.example .env
openssl rand -hex 32          # un secret par valeur vide du .env
nano .env
docker compose up -d --build
docker compose ps
```

Mise à jour :

```sh
cd ~/proxyhub && git pull
cd vps && docker compose up -d --build
```

Clé WireGuard NordVPN (jeton à générer sur my.nordaccount.com, configuration manuelle) :

```sh
curl -s -u token:<jeton> https://api.nordvpn.com/v1/users/services/credentials
# nordlynx_private_key -> WIREGUARD_PRIVATE_KEY
```

## Vérifier

```sh
./scripts/smoke-test.sh                                   # sur le VPS
curl -x http://<user>:<password>@<vps>:8081 https://api.ipify.org
python client/examples/vpn_exit.py                        # PROXYHUB_HOST, PROXY_USER, PROXY_PASSWORD
```

## API

```sh
TOKEN=<API_TOKEN du .env>
API=http://<vps>:8090
H="Authorization: Bearer $TOKEN"

curl $API/health                                          # sans jeton
curl -H "$H" $API/exits                                   # état, IP, pays
curl -H "$H" $API/exits/vpn-1/ip                          # IP mesurée maintenant
curl -H "$H" -X POST $API/exits/vpn-1/rotate              # nouvelle IP, même pays
curl -H "$H" -X POST "$API/exits/vpn-1/rotate?country=Belgium"
curl -H "$H" -X POST "$API/exits/vpn-1/rotate?force=true" # même avec des sessions ouvertes
curl -H "$H" $API/operations/<operation_id>               # résultat de la rotation
curl -H "$H" $API/exits/vpn-1/history                     # 7 derniers jours

curl -H "$H" -X POST $API/exits/vpn-1/sessions            # ouvre une session -> session_id
curl -H "$H" -X POST "$API/exits/vpn-1/sessions?country=Belgium"
curl -H "$H" -X DELETE $API/sessions/<session_id>         # ferme la session
```

- rotate : 202 + `operation_id`, `changed: false` si l'IP n'a pas bougé après `max_attempts`
- 409 : opération en cours (`operation_id`), sessions ouvertes (`active_sessions`), autre pays (`country`)
- 429 : délai minimal (`Retry-After`)
- session : expire seule après `session_ttl_seconds`, pas de rotation tant qu'une session est ouverte
- rotation auto : `rotate_every_sessions: N` dans `vps/config/exits.yaml`

## Client Python

```sh
pip install "git+https://github.com/YannKdev/ProxyHub#subdirectory=client"
```

```python
from proxyhub_client import ProxyHub

hub = ProxyHub("http://<vps>:8090", token="...", proxy_user="...", proxy_password="...")

with hub.session("vpn-1") as s:                    # requests.Session qui sort par vpn-1
    s.get("https://example.org")

with hub.session("vpn-1", country="Belgium") as s: # change de pays si besoin
    s.get("https://example.org")

hub.rotate("vpn-1")                                # attend le résultat
hub.rotate("vpn-1", wait=False)                    # renvoie l'operation_id
hub.exits()
```

- `hub.session` attend la fin d'une rotation en cours avant d'ouvrir la session
- exemple complet : `client/examples/hub_example.py`

## Tests

```sh
pip install -r tests/requirements.txt
pytest
ruff check . && ruff format --check .
```

## Notes

- HTTP simple : identifiants proxy et jeton API en clair sur le réseau
- Docker contourne ufw : `ufw deny` ne ferme pas les ports publiés
- serveur de contrôle Gluetun (8000) non publié, clé d'API
- pas d'en-têtes de proxy ajoutés (`HTTPPROXY_STEALTH`)

## Licence

MIT
