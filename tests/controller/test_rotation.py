import asyncio

import pytest
from app.hub import CooldownActive, ExitBusy, Hub
from app.store import Store
from fakes.fake_gluetun import FakeGluetun


@pytest.fixture
def rotate(new_hub):
    def run(fake, country=None, **overrides):
        async def scenario():
            hub = new_hub(fake, **overrides)
            op = hub.start_rotation("vpn-1", country)
            await hub.wait_operations()
            return hub, hub.store.operation(op["id"])

        return asyncio.run(scenario())

    return run


def test_rotation_changes_ip(rotate):
    hub, op = rotate(FakeGluetun(["1.1.1.1", "2.2.2.2"]))

    assert op["status"] == "done"
    assert op["result"]["old_ip"] == "1.1.1.1"
    assert op["result"]["new_ip"] == "2.2.2.2"
    assert op["result"]["changed"] is True
    assert op["result"]["attempts"] == 1
    assert hub.status["vpn-1"].state == "up"
    assert hub.status["vpn-1"].ip == "2.2.2.2"
    assert "vpn-1" not in hub.running


def test_same_ip_twice_retries(rotate):
    _, op = rotate(FakeGluetun(["1.1.1.1", "1.1.1.1", "2.2.2.2"]))

    assert op["result"]["changed"] is True
    assert op["result"]["attempts"] == 2


def test_unchanged_ip_gives_up_after_max_attempts(rotate):
    _, op = rotate(FakeGluetun(["1.1.1.1"]), max_attempts=3)

    assert op["status"] == "done"
    assert op["result"]["changed"] is False
    assert op["result"]["new_ip"] == "1.1.1.1"
    assert op["result"]["attempts"] == 3


def test_country_switch_uses_settings(rotate):
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"])
    _, op = rotate(fake, country="Belgium")

    assert op["result"]["country"] == "Belgium"
    assert fake.countries == ["Belgium"]
    assert ("PUT", "/v1/vpn/settings") in fake.requests
    assert ("PUT", "/v1/vpn/status") not in fake.requests


def test_current_country_keeps_settings(rotate):
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"], country="france")
    _, op = rotate(fake, country="France")

    assert op["result"]["changed"] is True
    assert ("PUT", "/v1/vpn/settings") not in fake.requests
    assert fake.requests.count(("PUT", "/v1/vpn/status")) == 2


def test_without_country_keeps_current_one(rotate):
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"], country="belgium")
    _, op = rotate(fake)

    assert op["result"]["country"] == "belgium"
    assert fake.countries == ["belgium"]


def test_unknown_country_fails(rotate):
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"])
    hub, op = rotate(fake, country="Atlantis")

    assert op["status"] == "failed"
    assert "Atlantis" in op["error"]
    assert fake.countries == ["france"]
    assert hub.status["vpn-1"].state == "up"


def test_gluetun_unreachable_fails(rotate):
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"])
    fake.reachable = False
    hub, op = rotate(fake)

    assert op["status"] == "failed"
    assert hub.status["vpn-1"].state == "down"
    assert "vpn-1" not in hub.running


def test_exit_not_coming_back_fails(rotate):
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"])
    fake.connects = False
    hub, op = rotate(fake, max_attempts=2)

    assert op["status"] == "failed"
    assert "did not come back" in op["error"]
    assert hub.status["vpn-1"].state == "down"


def test_second_rotation_while_running_is_refused(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1", "2.2.2.2"]))
        hub.start_rotation("vpn-1")
        with pytest.raises(ExitBusy):
            hub.start_rotation("vpn-1")
        await hub.wait_operations()

    asyncio.run(scenario())


def test_cooldown_between_rotations(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1", "2.2.2.2", "3.3.3.3"]), cooldown_seconds=60)
        hub.start_rotation("vpn-1")
        await hub.wait_operations()
        with pytest.raises(CooldownActive) as error:
            hub.start_rotation("vpn-1")
        return error.value.retry_after

    assert 0 < asyncio.run(scenario()) <= 60


def test_rotation_is_recorded_in_history(rotate):
    hub, _ = rotate(FakeGluetun(["1.1.1.1", "2.2.2.2"]))

    events = [entry["event"] for entry in hub.store.history("vpn-1", 10)]
    assert events == ["rotation"]


@pytest.fixture
def rotate_beside(settings, new_exit):
    """Rotation de vpn-1 alors que vpn-2 sort déjà par `other_ip`."""

    def run(fake, other_ip, **overrides):
        async def scenario():
            exits = {
                "vpn-1": new_exit(fake, **overrides),
                "vpn-2": new_exit(FakeGluetun([other_ip]), id="vpn-2", proxy_port=8082),
            }
            hub = Hub(settings, exits, Store(settings.db_path))
            hub.startup()
            hub.status["vpn-2"].ip = other_ip
            op = hub.start_rotation("vpn-1")
            await hub.wait_operations()
            return hub.store.operation(op["id"])

        return asyncio.run(scenario())

    return run


def test_rotation_skips_ip_of_another_exit(rotate_beside):
    op = rotate_beside(FakeGluetun(["1.1.1.1", "2.2.2.2", "3.3.3.3"]), other_ip="2.2.2.2")

    assert op["result"]["new_ip"] == "3.3.3.3"
    assert op["result"]["attempts"] == 2
    assert op["result"]["shared_with"] == []


def test_rotation_reports_ip_shared_with_another_exit(rotate_beside):
    op = rotate_beside(FakeGluetun(["1.1.1.1", "2.2.2.2"]), other_ip="2.2.2.2", max_attempts=2)

    assert op["result"]["new_ip"] == "2.2.2.2"
    assert op["result"]["changed"] is True
    assert op["result"]["shared_with"] == ["vpn-2"]
