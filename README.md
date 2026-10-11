# proxyhub

Passerelle proxy HTTP sur un VPS, avec plusieurs sorties. Le port choisit la sortie :

| Port | Sortie |
|---|---|
| 8080 | téléphone Android en 4G, via un Raspberry Pi |
| 8081 | NordVPN, via Gluetun |

Pour l'instant, seule la sortie VPN est en place.

## Installation sur le VPS

Serveur Linux avec Docker et plugin Compose, abonnement NordVPN.

```sh
git clone https://github.com/<pseudo>/proxyhub.git
cd proxyhub/vps
cp .env.example .env
```

Remplir `.env`. Tant qu'une valeur obligatoire est vide, `docker compose` refuse de démarrer. Les secrets se génèrent avec `openssl rand -hex 32`.

```sh
docker compose up -d
docker compose logs -f gluetun
```

### Clé WireGuard NordVPN

1. Sur <https://my.nordaccount.com/>, section NordVPN, configuration manuelle : générer un jeton d'accès.
2. Récupérer la clé :

   ```sh
   curl -s -u token:<jeton> https://api.nordvpn.com/v1/users/services/credentials
   ```

3. Copier la valeur de `nordlynx_private_key` dans `WIREGUARD_PRIVATE_KEY`.

## Vérifier

Sur le VPS :

```sh
./scripts/smoke-test.sh
```

Depuis une autre machine :

```sh
curl -x http://<user>:<password>@<vps>:8081 https://api.ipify.org
```

En Python, voir `client/examples/vpn_exit.py`.

## Sécurité

- Le proxy fonctionne en HTTP simple : les identifiants passent en clair entre le client et le VPS. Le contenu des sites en HTTPS reste chiffré de bout en bout.
- Seul le port 8081 est publié. Le serveur de contrôle de Gluetun reste sur le réseau Docker interne, protégé par une clé d'API.
- Le proxy n'ajoute pas d'en-têtes de proxy aux requêtes : l'IP du client n'est pas transmise aux sites.

## Licence

MIT
