from pathlib import Path

import pytest
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


@pytest.mark.parametrize("name", list(ENV))
def test_missing_secret_is_refused(name):
    with pytest.raises(ConfigError, match=name):
        load_settings(ENV | {name: ""})


def test_short_token_is_refused():
    with pytest.raises(ConfigError, match="API_TOKEN"):
        load_settings(ENV | {"API_TOKEN": "short"})


def test_shipped_exits_file_is_valid():
    exits = load_exits(ROOT / "vps" / "config" / "exits.yaml")
    assert [e.id for e in exits] == ["vpn-1"]


def test_duplicate_exit_ids_are_refused(tmp_path):
    entry = (
        "  - {id: vpn-1, type: vpn, proxy_port: 8081, proxy_url: http://g:8888, "
        "gluetun_control_url: http://g:8000}\n"
    )
    path = tmp_path / "exits.yaml"
    path.write_text("exits:\n" + entry + entry)
    with pytest.raises(ConfigError, match="duplicate"):
        load_exits(path)


def test_unknown_field_is_refused(tmp_path):
    path = tmp_path / "exits.yaml"
    path.write_text(
        "exits:\n  - {id: vpn-1, type: vpn, proxy_port: 8081, proxy_url: http://g:8888, "
        "gluetun_control_url: http://g:8000, cooldown: 5}\n"
    )
    with pytest.raises(ConfigError):
        load_exits(path)
