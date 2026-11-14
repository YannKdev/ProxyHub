# proxyhub

Passerelle proxy HTTP hébergée sur un VPS, pour faire sortir des scripts par une IP choisie. Le client utilise le VPS comme un proxy classique sur un seul port, et choisit sa sortie (NordVPN, CyberGhost, plus tard mobile 4G), son pays et sa session dans le nom d'utilisateur du proxy. Une session garde sa sortie, donc son IP ; une nouvelle session donne une autre IP. Une API d'administration montre l'état des sorties, leur historique et les sessions.

Le téléphone est derrière le NAT de l'opérateur : c'est le Raspberry Pi qui ouvre un tunnel vers le VPS, jamais l'inverse.

```text
scripts ──> VPS:8080 (proxy d'entrée) ──> Gluetun ──> NordVPN         vpn-1
                                      ──> Gluetun ──> CyberGhost      cyberghost-1 (facultatif)
                                      ──> Gluetun ──> CyberGhost      cyberghost-2 à -4 (facultatifs)
                                      ──> tunnel SSH ──> Pi ──> 4G    (à venir)
admin   ──> https://<domaine> ──> Caddy ──> API : état, IP, historique, sessions
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

Ports à ouvrir : 80 et 443 (Caddy, certificat Let's Encrypt), 8080 (proxy d'entrée). 8081 à 8083 : accès direct à une sortie, publics pendant la migration des scripts (8084 et 8085 : depuis le VPS seulement).

Mise à jour (pull, vérification du `.env`, reconstruction, smoke test) :

```sh
cd ~/proxyhub/vps && ./scripts/update.sh
```

Clé WireGuard NordVPN (jeton à générer sur my.nordaccount.com, configuration manuelle) :

```sh
curl -s -u token:<jeton> https://api.nordvpn.com/v1/users/services/credentials
# nordlynx_private_key -> WIREGUARD_PRIVATE_KEY
```

Sorties CyberGhost (facultatives, OpenVPN seulement dans Gluetun) :

- espace client CyberGhost : Manage Devices > Manual setup > Configure Device, en OpenVPN
- identifiants OpenVPN générés (pas ceux du compte) → `CYBERGHOST_USER`, `CYBERGHOST_PASSWORD`
- certificat et clé client : `client.crt` et `client.key` du zip, ou blocs `<cert>` et `<key>` du `.ovpn`

```sh
openssl x509 -in client.crt | sed '1d;$d' | tr -d '\n'   # -> CYBERGHOST_CERT
openssl pkey -in client.key | sed '1d;$d' | tr -d '\n'   # -> CYBERGHOST_KEY
# depuis le .ovpn :
sed -n '/<cert>/,/<\/cert>/p' openvpn.ovpn | openssl x509 | sed '1d;$d' | tr -d '\n'
sed -n '/<key>/,/<\/key>/p' openvpn.ovpn | openssl pkey | sed '1d;$d' | tr -d '\n'

nano .env                     # décommenter le bloc CyberGhost, COMPOSE_PROFILES=cyberghost-1,...,cyberghost-4
./scripts/update.sh           # ou docker compose up -d : démarre les gluetun-cyberghost-N

# liste de serveurs intégrée à Gluetun périmée (IP qui ne répondent plus) : la mettre à jour
# une fois après la première installation, ensuite UPDATER_PERIOD le fait toutes les 24 h
# (à refaire pour chaque nouvelle sortie : son volume part de la liste intégrée)
for s in $(docker compose ps --services | grep cyberghost); do
  docker compose exec -T $s /gluetun-entrypoint update -enduser -providers cyberghost </dev/null
  docker compose restart $s
done
```

- profils = id des sorties : `cyberghost-1` à `cyberghost-4`, au choix
- `cyberghost-N` reprend les `CYBERGHOST_*`, sauf les `CYBERGHOST_N_*` définis (autre configuration manuelle)
- une même configuration manuelle sert plusieurs connexions simultanées (testé avec deux)
- désactiver : retirer le profil de `COMPOSE_PROFILES`, puis `docker compose rm -sf gluetun-cyberghost-2 && docker compose up -d`
- pays de départ : `CYBERGHOST_COUNTRIES`, `CYBERGHOST_N_COUNTRIES` (France par défaut)
- 7 connexions simultanées par abonnement : chaque conteneur en prend une, quel que soit le nombre de scripts derrière

## Proxy d'entrée

Un seul compte (`PROXY_USER`, `PROXY_PASSWORD`), options dans le nom d'utilisateur.

```sh
curl -x http://USER:PASS@VPS:8080 https://api.ipify.org                    # une sortie VPN, gardée par IP du client
curl -x http://USER-session-a1:PASS@VPS:8080 https://api.ipify.org         # session a1 : même IP à chaque appel
curl -x http://USER-session-a2:PASS@VPS:8080 https://api.ipify.org         # nouvelle session : autre IP
curl -x http://USER-session-a1-ip-2:PASS@VPS:8080 https://api.ipify.org    # a1 change d'IP, lâche sa sortie
curl -x http://USER-pool-cyberghost-country-be-session-x:PASS@VPS:8080 https://api.ipify.org
curl -x http://USER-session-x-hold-60:PASS@VPS:8080 https://api.ipify.org  # sortie gardée 60 min sans trafic
```

```python
import requests
from urllib.parse import quote

user = "USER-pool-cyberghost-country-be-session-job1-ip-1"     # options du tableau, toutes facultatives
proxy = f"http://{quote(user, safe='')}:{quote('PASS', safe='')}@VPS:8080"
proxies = {"http": proxy, "https": proxy}

print(requests.get("https://api.ipify.org", proxies=proxies, timeout=60).text)  # IP de sortie
# même nom = même IP ; ip-2, ip-3… = IP neuve pour la même session
```

| Option | Valeurs |
| --- | --- |
| `pool` | `nordvpn`, `cyberghost`, `vpn` (toutes les sorties VPN, par défaut), `mobile` (à venir) |
| `country` | code à deux lettres : `fr`, `be`, `de`, `gb`… |
| `session` | 1 à 64 lettres, chiffres ou `_`, choisi par le client |
| `ip` | numéro d'IP de la session : un numéro plus grand donne une IP neuve, un plus petit est refusé (409) |
| `hold` | minutes de réservation sans trafic (5 par défaut, 1440 au plus) |

- une session = une sortie réservée, que personne d'autre ne fait tourner
- changer d'IP souvent : garder la session et augmenter `ip` (une sortie au plus par session) ; une nouvelle `session` garde l'ancienne réservée 5 min
- même `session` avec un autre `pool` ou `country` : autre session
- libérée après 5 min sans connexion (ou `hold`), puis la sortie tourne
- pays absent des sorties libres : une sortie change de pays (≈ 20 s à la première connexion)
- autant de sessions simultanées que de sorties ; IP gardée au mieux (une panne du VPN la change)
- sortie en panne à la première connexion d'une IP : le proxy passe seul sur une autre sortie
- le message d'erreur est aussi dans la ligne de statut (`502 exit unreachable`)

| Code | Cause |
| --- | --- |
| 400 | option inconnue ou mal écrite |
| 409 | `ip` plus petit que celui de la session |
| 403 | port autre que 80 et 443, adresse privée |
| 407 | identifiants faux |
| 429 | IP bloquée 30 min après 5 mauvais mots de passe (proxy et API ensemble) |
| 502 | sortie en panne (après un relais : passer à l'`ip` suivante), hôte injoignable par la sortie (IPv6 seulement…), pays injoignable |
| 503 | plus de sortie libre, réessayer (`Retry-After`) |
| 504 | sortie pas prête à temps, réessayer |

## Vérifier

```sh
./scripts/smoke-test.sh                                   # sur le VPS, dans ~/proxyhub/vps
curl -x http://<user>-session-test:<password>@<vps>:8080 https://api.ipify.org
curl -x http://<user>:<password>@<vps>:8081 https://api.ipify.org   # accès direct à vpn-1
python client/examples/proxy_sessions.py                  # PROXYHUB_HOST, PROXY_USER, PROXY_PASSWORD
```

## API d'administration

```sh
TOKEN=<API_TOKEN du .env>
API=https://<API_DOMAIN>                                  # sur le VPS : http://127.0.0.1:8090
H="Authorization: Bearer $TOKEN"

curl $API/health                                          # sans jeton
curl -H "$H" $API/exits                                   # état, IP, pays, session qui tient la sortie
curl -H "$H" $API/sessions                                # sessions du proxy d'entrée
curl -H "$H" -X DELETE $API/sessions/<id>                 # libère une session (coupe ses connexions)
curl -H "$H" $API/exits/vpn-1/ip                          # IP mesurée maintenant
curl -H "$H" -X POST $API/exits/vpn-1/rotate              # nouvelle IP, même pays
curl -H "$H" -X POST "$API/exits/vpn-1/rotate?country=be" # code ou nom (Belgium)
curl -H "$H" -X POST "$API/exits/vpn-1/rotate?force=true" # même si une session tient la sortie
curl -H "$H" $API/operations/<operation_id>               # résultat de la rotation
curl -H "$H" $API/exits/vpn-1/history                     # 7 derniers jours
```

- rotate : 202 + `operation_id`, `changed: false` si l'IP n'a pas bougé après `max_attempts`
- rotate évite aussi l'IP des autres sorties ; si elle n'y arrive pas : `shared_with: ["cyberghost-1"]`
- 409 : opération en cours (`operation_id`), sortie tenue par une session (`session_id`)
- 429 : délai minimal, ou IP bloquée 30 min après 5 mauvais jetons (`Retry-After`)
- `params.trigger` des opérations : `api`, `proxy` (préparation d'une session), `release` (après libération), `auto` (reconnexion)
- reconnexion auto : sortie `down` depuis 2 min → le contrôleur relance le VPN (au plus toutes les 5 min), état `recovering`
- états : `up`, `down`, `rotating`, `recovering`

## Client Python

```sh
pip install "git+https://github.com/YannKdev/ProxyHub#subdirectory=client"
```

```python
from proxyhub_client import ProxyHub

hub = ProxyHub("https://<API_DOMAIN>", token="...", proxy_user="...", proxy_password="...")

with hub.proxy(session="job1") as s:                       # requests.Session par le port 8080
    s.get("https://example.org")

with hub.proxy(session="job2", pool="cyberghost", country="be", hold=30) as s:
    s.get("https://example.org")

with hub.proxy(session="job1", ip=2) as s:                 # même session, IP neuve
    s.get("https://example.org")

hub.proxy_url(session="job3")                              # URL du proxy, pour un autre outil
hub.exits()                                                # administration : jeton nécessaire
hub.sessions()
hub.release("<id>")
hub.rotate("vpn-1")                                        # attend le résultat
```

- sans `token`, seul le proxy (`proxy`, `proxy_url`) est utilisable
- exemple complet : `client/examples/hub_example.py`

## Exploitation (sur le VPS, dans `~/proxyhub/vps`)

```sh
H="Authorization: Bearer $(grep '^API_TOKEN=' .env | cut -d= -f2-)"
docker compose ps                                         # état des conteneurs
docker compose logs -f --tail 50 controller               # ou gluetun, gluetun-cyberghost-1, caddy
curl -s -H "$H" http://127.0.0.1:8090/exits               # API sans passer par Caddy
curl -s -H "$H" http://127.0.0.1:8090/sessions            # qui tient quelle sortie
curl -s -H "$H" http://127.0.0.1:8090/exits/vpn-1/history # coupures, reconnexions, rotations
grep -E '^(API_TOKEN|PROXY_USER|PROXY_PASSWORD)=' .env    # secrets à donner aux clients
docker compose restart gluetun                            # relancer le VPN (≈ 30 s de coupure)
docker system df                                          # place prise par Docker
```

Changer un secret : modifier `.env`, puis `docker compose up -d`.

- `PROXY_*` : les Gluetun et le contrôleur redémarrent (≈ 30 s de coupure), mettre à jour les clients
- `API_TOKEN` : seul le contrôleur redémarre, mettre à jour les clients
- `WIREGUARD_PRIVATE_KEY` : nouvelle clé NordLynx (voir plus haut)
- `CYBERGHOST_*` : `gluetun-cyberghost-1` redémarre, et `-2` s'il reprend ces valeurs
- `GLUETUN_API_KEY` : tous les Gluetun et le contrôleur redémarrent

Un redémarrage du contrôleur coupe les connexions du proxy d'entrée et oublie les sessions.

Après un redémarrage du VPS, tout repart seul (`restart: unless-stopped`, Docker lancé au boot). La sortie peut être vue `down` pendant la première minute.

## Dépannage

- `vpn-1` reste `down` : `docker compose logs --tail 100 gluetun`. La reconnexion auto essaie après 2 min, puis toutes les 5 min. Sinon `docker compose restart gluetun`.
- Gluetun « unhealthy » juste après un démarrage : il change de serveur jusqu'à ce que son contrôle passe, en général moins d'une minute.
- L'IP ne change pas à la rotation : Gluetun est retombé sur le même serveur, `changed: false`. Relancer la rotation, ou changer de pays.
- 503 « no free exit » sur le port 8080 : toutes les sorties du groupe sont tenues. `GET /sessions`, libérer une session inutile, ou attendre ses 5 min d'inactivité.
- Une session ne se libère pas : une connexion reste ouverte (navigateur, keep-alive). Elle est coupée après 5 min sans trafic, sinon `DELETE /sessions/<id>`.
- Un script en accès direct (8081 à 8083) voit son IP changer : une session du port 8080 a pris puis libéré cette sortie, qui a tourné. Passer le script sur le port 8080.
- 429 « too many failed authentications » : IP bloquée 30 min. Attendre, ou `docker compose restart controller` (le blocage est en mémoire).
- API injoignable en HTTPS : `docker compose logs caddy`, enregistrement DNS A vers le VPS, ports 80 et 443 ouverts. Sur le VPS : `curl http://127.0.0.1:8090/health`.
- Certificat non renouvelé : `docker compose logs caddy | grep -i certificate`. Caddy renouvelle seul, il a besoin du port 80 ou 443.
- `missing in .env: X` à la mise à jour : ajouter `X` (voir `.env.example`), relancer `update.sh`.
- `cyberghost-1` absent de `/exits` : profil `cyberghost-1` absent de `COMPOSE_PROFILES` dans `.env`, ou contrôleur pas recréé (`docker compose up -d`).
- `gluetun-cyberghost-1` redémarre en boucle : `docker compose logs --tail 50 gluetun-cyberghost-1`. « missing value » : un `CYBERGHOST_*` vide. « Cannot load inline certificate » : certificat mal copié (base64 seul, une ligne), idem pour la clé. « AUTH_FAILED » : identifiants du compte au lieu des identifiants OpenVPN.
- « TLS key negotiation failed » sur CyberGhost (souvent un seul pays) : liste de serveurs périmée, la mettre à jour (section CyberGhost).
- Rotation vers un pays qui ne répond pas : la sortie revient au pays d'avant, l'opération finit en `failed` (« switched back to … »).
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
- proxy d'entrée : CONNECT et HTTP simple, ports 80 et 443 seulement (`PROXY_CONNECT_PORTS`), une requête par connexion en HTTP simple
- journaux du proxy : hôte visé, sortie, session, code, durée ; jamais d'identifiants ni de contenu
- Docker contourne ufw : `ufw deny` ne ferme pas les ports publiés
- serveur de contrôle Gluetun (8000) non publié, clé d'API
- pas d'en-têtes de proxy ajoutés (`HTTPPROXY_STEALTH`)
- liste des serveurs mise à jour chaque jour (`UPDATER_PERIOD`, un volume `gluetun-*-data` par Gluetun)
- une sortie = un conteneur Gluetun + une entrée dans `vps/config/exits.yaml` (`pool`, et `profile` pour une sortie facultative)
- journaux Docker : 3 × 10 Mo max par conteneur

## Licence

MIT
