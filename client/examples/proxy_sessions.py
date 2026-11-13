# Proxy d'entrée sans le module client : même session = même IP, nouvelle session = autre IP.
# Variables d'environnement : PROXYHUB_HOST, PROXY_USER, PROXY_PASSWORD.
import os
from urllib.parse import quote

import requests

IP_SERVICE = "https://api.ipify.org"

host = os.environ.get("PROXYHUB_HOST", "localhost")
user = os.environ["PROXY_USER"]
password = quote(os.environ["PROXY_PASSWORD"], safe="")


def ip_through(options: str) -> str:
    proxy = f"http://{quote(user + options, safe='')}:{password}@{host}:8080"
    response = requests.get(IP_SERVICE, proxies={"http": proxy, "https": proxy}, timeout=60)
    response.raise_for_status()
    return response.text


print("direct:          ", requests.get(IP_SERVICE, timeout=15).text)
print("session a:       ", ip_through("-session-a"))
print("session a again: ", ip_through("-session-a"))
print("session b:       ", ip_through("-session-b"))
