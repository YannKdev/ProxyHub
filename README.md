# proxyhub

Passerelle proxy HTTP hébergée sur un VPS, pour faire sortir des scripts par une IP choisie. Le client utilise le VPS comme un proxy classique, et le port choisit la sortie : IP mobile 4G ou IP NordVPN (ou autre). Une API permet de changer d'IP (ou de pays pour le VPN) à la demande et garde l'historique des IP.

Le téléphone est derrière le NAT de l'opérateur : c'est le Raspberry Pi qui ouvre un tunnel vers le VPS, jamais l'inverse.

```text
scripts ──> VPS:8080 ──> tunnel SSH ──> Raspberry Pi ──> téléphone 4G    (à venir)
            VPS:8081 ──> Gluetun ──> NordVPN
            https://<domaine> ──> Caddy ──> API : état, IP, rotation
```

## Installation (VPS)

```sh
git clone https://github.com/YannKdev/ProxyHub.git proxyhub
cd proxyhub/vps
cp .env.example .env
openssl rand -hex 32          # un secret par valeur vide du .env
nano .env                     # API_DOMAIN : sous-domaine avec un enregistrement A vers le VPS
docker compose up -d --build
docker compose ps
```

Ports à ouvrir : 80 et 443 (Caddy, certificat Let's Encrypt), 8081 (proxy VPN).

Mise à jour (pull, vérification du `.env`, reconstruction, smoke test) :

```sh
cd ~/proxyhub/vps && ./scripts/update.sh
```

Clé WireGuard NordVPN (jeton à générer sur my.nordaccount.com, configuration manuelle) :

```sh
curl -s -u token:<jeton> https://api.nordvpn.com/v1/users/services/credentials
# nordlynx_private_key -> WIREGUARD_PRIVATE_KEY
```

## Vérifier

```sh
./scripts/smoke-test.sh                                   # sur le VPS, dans ~/proxyhub/vps
curl -x http://<user>:<password>@<vps>:8081 https://api.ipify.org
python client/examples/vpn_exit.py                        # PROXYHUB_HOST, PROXY_USER, PROXY_PASSWORD
```

## API

```sh
TOKEN=<API_TOKEN du .env>
API=https://<API_DOMAIN>                                  # sur le VPS : http://127.0.0.1:8090
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
- 429 : délai minimal, ou IP bloquée 30 min après 5 mauvais jetons (`Retry-After`)
- session : expire seule après `session_ttl_seconds`, pas de rotation tant qu'une session est ouverte
- rotation auto : `rotate_every_sessions: N` dans `vps/config/exits.yaml`
- reconnexion auto : sortie `down` depuis 2 min → le contrôleur relance le VPN (au plus toutes les 5 min), état `recovering`
- états : `up`, `down`, `rotating`, `recovering`

## Client Python

```sh
pip install "git+https://github.com/YannKdev/ProxyHub#subdirectory=client"
```

```python
from proxyhub_client import ProxyHub

hub = ProxyHub("https://<API_DOMAIN>", token="...", proxy_user="...", proxy_password="...")

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

## Exploitation (sur le VPS, dans `~/proxyhub/vps`)

```sh
H="Authorization: Bearer $(grep '^API_TOKEN=' .env | cut -d= -f2-)"
docker compose ps                                         # état des conteneurs
docker compose logs -f --tail 50 controller               # ou gluetun, caddy
curl -s -H "$H" http://127.0.0.1:8090/exits               # API sans passer par Caddy
curl -s -H "$H" http://127.0.0.1:8090/exits/vpn-1/history # coupures, reconnexions, rotations
grep -E '^(API_TOKEN|PROXY_USER|PROXY_PASSWORD)=' .env    # secrets à donner aux clients
docker compose restart gluetun                            # relancer le VPN (≈ 30 s de coupure)
docker system df                                          # place prise par Docker
```

Changer un secret : modifier `.env`, puis `docker compose up -d`.

- `PROXY_*` : Gluetun et le contrôleur redémarrent (≈ 30 s de coupure), mettre à jour les clients
- `API_TOKEN` : seul le contrôleur redémarre, mettre à jour les clients
- `WIREGUARD_PRIVATE_KEY` : nouvelle clé NordLynx (voir plus haut)

Après un redémarrage du VPS, tout repart seul (`restart: unless-stopped`, Docker lancé au boot). La sortie peut être vue `down` pendant la première minute.

## Dépannage

- `vpn-1` reste `down` : `docker compose logs --tail 100 gluetun`. La reconnexion auto essaie après 2 min, puis toutes les 5 min. Sinon `docker compose restart gluetun`.
- Gluetun « unhealthy » juste après un démarrage : il change de serveur jusqu'à ce que son contrôle passe, en général moins d'une minute.
- L'IP ne change pas à la rotation : Gluetun est retombé sur le même serveur, `changed: false`. Relancer la rotation, ou changer de pays.
- 429 « too many failed authentications » : IP bloquée 30 min. Attendre, ou `docker compose restart controller` (le blocage est en mémoire).
- API injoignable en HTTPS : `docker compose logs caddy`, enregistrement DNS A vers le VPS, ports 80 et 443 ouverts. Sur le VPS : `curl http://127.0.0.1:8090/health`.
- Certificat non renouvelé : `docker compose logs caddy | grep -i certificate`. Caddy renouvelle seul, il a besoin du port 80 ou 443.
- `missing in .env: X` à la mise à jour : ajouter `X` (voir `.env.example`), relancer `update.sh`.
- Une autre installation Gluetun utilise la même clé NordVPN : si deux conteneurs tombent sur le même serveur, l'un perd sa connexion. La reconnexion auto le répare.

## Tests

```sh
pip install -r tests/requirements.txt
pytest
ruff check . && ruff format --check .
```

## Notes

- API en HTTPS (Caddy), port 8090 joignable seulement depuis le VPS
- proxys en HTTP simple : leurs identifiants passent en clair sur le réseau
- Docker contourne ufw : `ufw deny` ne ferme pas les ports publiés
- serveur de contrôle Gluetun (8000) non publié, clé d'API
- pas d'en-têtes de proxy ajoutés (`HTTPPROXY_STEALTH`)
- liste des serveurs NordVPN mise à jour chaque jour (`UPDATER_PERIOD`, volume `gluetun-data`)
- journaux Docker : 3 × 10 Mo max par conteneur

## Licence

MIT
