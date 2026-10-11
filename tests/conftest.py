import pytest
from app.config import Settings, VpnExitConfig
from app.hub import Hub
from app.store import Store
from app.vpn import VpnExit
from fakes.fake_gluetun import FakeGluetun

API_TOKEN = "t" * 40


@pytest.fixture
def api_token() -> str:
    return API_TOKEN


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        api_token=API_TOKEN,
        proxy_user="user",
        proxy_password="password",
        gluetun_api_key="gluetun-key",
        db_path=tmp_path / "proxyhub.db",
        monitor_seconds=0,
        session_sweep_seconds=0,
    )


@pytest.fixture
def new_exit():
    def factory(fake: FakeGluetun, **overrides) -> VpnExit:
        options = {
            "id": "vpn-1",
            "type": "vpn",
            "proxy_port": 8081,
            "proxy_url": "http://gluetun:8888",
            "gluetun_control_url": "http://gluetun:8000",
            "cooldown_seconds": 0,
            "ready_timeout_seconds": 0.05,
            "poll_seconds": 0.01,
        }
        control, web = fake.clients()
        return VpnExit(
            VpnExitConfig(**(options | overrides)), control, web, "https://api.ipify.org"
        )

    return factory


@pytest.fixture
def new_hub(settings, new_exit):
    def factory(fake: FakeGluetun, **overrides) -> Hub:
        hub = Hub(settings, {"vpn-1": new_exit(fake, **overrides)}, Store(settings.db_path))
        hub.startup()
        return hub

    return factory
