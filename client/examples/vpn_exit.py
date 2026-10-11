# Affiche l'IP directe puis l'IP vue à travers la sortie VPN.
# Variables d'environnement : PROXYHUB_HOST, PROXY_USER, PROXY_PASSWORD.
import os
from urllib.parse import quote

import requests

IP_SERVICE = "https://api.ipify.org"

host = os.environ.get("PROXYHUB_HOST", "localhost")
user = quote(os.environ["PROXY_USER"], safe="")
password = quote(os.environ["PROXY_PASSWORD"], safe="")
proxy = f"http://{user}:{password}@{host}:8081"

print("direct:", requests.get(IP_SERVICE, timeout=15).text)

response = requests.get(IP_SERVICE, proxies={"http": proxy, "https": proxy}, timeout=30)
response.raise_for_status()
print("vpn-1: ", response.text)
