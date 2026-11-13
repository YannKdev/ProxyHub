from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from app.config import ConfigError, load_exits, load_settings

ROOT = Path(__file__).resolve().parents[2]
ENV = {
    "API_TOKEN": "t" * 32,
    "PROXY_USER": "user",
    "PROXY_PASSWORD": "password",
    "GLUETUN_API_KEY": "key",
}


def test_settings_from_env():
    settings = load_settings(ENV | {"MONITOR_SECONDS": "5", "HISTORY_DAYS": "3"})
    assert settings.monitor_seconds == 5
    assert settings.history_days == 3
    assert settings.ip_check_url == "https://api.ipify.org"
    assert settings.compose_profiles == []


def test_proxy_settings_from_env():
    settings = load_settings(
        ENV
        | {"PROXY_PORT": "9000", "PROXY_CONNECT_PORTS": "443, 8443", "SESSION_IDLE_SECONDS": "60"}
    )
    assert settings.proxy_port == 9000
    assert settings.proxy_connect_ports == [443, 8443]
    assert settings.session_idle_seconds == 60
    assert load_settings(ENV).proxy_connect_ports == [80, 443]


CYBERGHOST = [f"cyberghost-{n}" for n in range(1, 5)]


def test_shipped_exits_have_pools():
    path = ROOT / "vps" / "config" / "exits.yaml"
    pools = {e.id: e.pool for e in load_exits(path, CYBERGHOST)}
    assert pools == {"vpn-1": "nordvpn"} | dict.fromkeys(CYBERGHOST, "cyberghost")


def test_compose_profiles_from_env():
    settings = load_settings(ENV | {"COMPOSE_PROFILES": "cyberghost-1, other,"})
    assert settings.compose_profiles == ["cyberghost-1", "other"]


@pytest.mark.parametrize("name", list(ENV))
def test_missing_secret_is_refused(name):
    with pytest.raises(ConfigError, match=name):
        load_settings(ENV | {name: ""})


def test_short_token_is_refused():
    with pytest.raises(ConfigError, match="API_TOKEN"):
        load_settings(ENV | {"API_TOKEN": "short"})


def test_shipped_exits_file_is_valid():
    path = ROOT / "vps" / "config" / "exits.yaml"
    assert [e.id for e in load_exits(path)] == ["vpn-1"]
    assert [e.id for e in load_exits(path, ["cyberghost-2"])] == ["vpn-1", "cyberghost-2"]
    all_exits = load_exits(path, CYBERGHOST)
    assert [e.id for e in all_exits] == ["vpn-1", *CYBERGHOST]
    assert len({e.proxy_port for e in all_exits}) == 5


def test_shipped_profiles_match_compose():
    compose = yaml.safe_load((ROOT / "vps" / "docker-compose.yml").read_text(encoding="utf-8"))
    compose_profiles = {p for s in compose["services"].values() for p in s.get("profiles", [])}
    exits = load_exits(ROOT / "vps" / "config" / "exits.yaml", compose_profiles)
    for exit in exits:
        proxy = urlsplit(exit.proxy_url)
        service = compose["services"][proxy.hostname]
        assert urlsplit(exit.gluetun_control_url).hostname == proxy.hostname
        # Publié pour tous ("8081:8888") ou pour le VPS seul ("127.0.0.1:8084:8888").
        published = {port.removeprefix("127.0.0.1:") for port in service["ports"]}
        assert f"{exit.proxy_port}:{proxy.port}" in published
        assert service.get("profiles", [None]) == [exit.profile]


def test_exit_with_inactive_profile_is_skipped(tmp_path):
    path = tmp_path / "exits.yaml"
    path.write_text(
        "exits:\n"
        "  - {id: vpn-1, type: vpn, pool: p, proxy_port: 8081, proxy_url: http://g:8888, "
        "gluetun_control_url: http://g:8000}\n"
        "  - {id: vpn-2, type: vpn, pool: p, profile: extra, proxy_port: 8082, "
        "proxy_url: http://h:8888, gluetun_control_url: http://h:8000}\n"
    )
    assert [e.id for e in load_exits(path, ["other"])] == ["vpn-1"]
    assert [e.id for e in load_exits(path, ["other", "extra"])] == ["vpn-1", "vpn-2"]


def test_duplicate_exit_ids_are_refused(tmp_path):
    entry = (
        "  - {id: vpn-1, type: vpn, pool: p, proxy_port: 8081, proxy_url: http://g:8888, "
        "gluetun_control_url: http://g:8000}\n"
    )
    path = tmp_path / "exits.yaml"
    path.write_text("exits:\n" + entry + entry)
    with pytest.raises(ConfigError, match="duplicate"):
        load_exits(path)


def test_unknown_field_is_refused(tmp_path):
    path = tmp_path / "exits.yaml"
    path.write_text(
        "exits:\n  - {id: vpn-1, type: vpn, pool: p, proxy_port: 8081, proxy_url: http://g:8888, "
        "gluetun_control_url: http://g:8000, cooldown: 5}\n"
    )
    with pytest.raises(ConfigError):
        load_exits(path)
