# Sessions et rotation avec le module client.
# Variables d'environnement : PROXYHUB_HOST, PROXYHUB_TOKEN, PROXY_USER, PROXY_PASSWORD.
import os

from proxyhub_client import ProxyHub

hub = ProxyHub(
    base_url=f"http://{os.environ['PROXYHUB_HOST']}:8090",
    token=os.environ["PROXYHUB_TOKEN"],
    proxy_user=os.environ["PROXY_USER"],
    proxy_password=os.environ["PROXY_PASSWORD"],
)

for info in hub.exits():
    print(info["id"], info["state"], info["ip"], info["country"])

with hub.session("vpn-1") as s:
    print(s.proxyhub["session_id"], "->", s.get("https://api.ipify.org", timeout=30).text)

with hub.session("vpn-1", country="Belgium") as s:
    print("Belgium ->", s.get("https://api.ipify.org", timeout=30).text)

print(hub.rotate("vpn-1", country="France"))
