# Proxy d'entrée et administration avec le module client.
# Variables d'environnement : PROXYHUB_URL (ex. https://api.example.com), PROXYHUB_TOKEN,
# PROXY_USER, PROXY_PASSWORD.
import os

from proxyhub_client import ProxyHub

IP_SERVICE = "https://api.ipify.org"

hub = ProxyHub(
    base_url=os.environ["PROXYHUB_URL"],
    token=os.environ["PROXYHUB_TOKEN"],
    proxy_user=os.environ["PROXY_USER"],
    proxy_password=os.environ["PROXY_PASSWORD"],
)

for info in hub.exits():
    print(info["id"], info["pool"], info["state"], info["ip"], info["country"])

with hub.proxy(session="example1") as s:
    print("session example1:", s.get(IP_SERVICE, timeout=30).text)
    print("same session:    ", s.get(IP_SERVICE, timeout=30).text)

with hub.proxy(session="example1", ip=2) as s:
    print("example1, ip 2:  ", s.get(IP_SERVICE, timeout=60).text)

with hub.proxy(session="example2", pool="cyberghost", country="be", hold=10) as s:
    print("session example2 (cyberghost, be):", s.get(IP_SERVICE, timeout=60).text)

for session in hub.sessions():
    print("open:", session["id"], session["label"], session["exit"])
    hub.release(session["id"])
