import asyncio
from datetime import timedelta

from app.store import Store, utcnow
from fakes.fake_gluetun import FakeGluetun


def test_check_marks_exit_up(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1"]))
        await hub.check_all()
        return hub.status["vpn-1"]

    status = asyncio.run(scenario())
    assert status.state == "up"
    assert status.ip == "1.1.1.1"
    assert status.country == "france"
    assert status.checked_at is not None


def test_check_detects_down_and_ip_change(new_hub):
    fake = FakeGluetun(["1.1.1.1"])

    async def scenario():
        hub = new_hub(fake)
        await hub.check_all()
        fake.reachable = False
        await hub.check_all()
        down = hub.status["vpn-1"].state
        fake.reachable = True
        fake.ip = "9.9.9.9"
        await hub.check_all()
        return hub, down

    hub, down = asyncio.run(scenario())
    assert down == "down"
    assert hub.status["vpn-1"].state == "up"
    events = [(e["event"], e.get("state") or e.get("ip")) for e in hub.store.history("vpn-1", 10)]
    assert ("state", "down") in events
    assert ("ip", "9.9.9.9") in events


def test_check_skips_exit_during_rotation(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1"]))
        hub.status["vpn-1"].state = "rotating"
        hub.running["vpn-1"] = "operation"
        await hub.check_all()
        return hub.status["vpn-1"].state

    assert asyncio.run(scenario()) == "rotating"


def test_startup_fails_interrupted_operations(settings, new_hub):
    op = Store(settings.db_path).create_operation("rotate", "vpn-1", {"country": None})
    hub = new_hub(FakeGluetun(["1.1.1.1"]))

    interrupted = hub.store.operation(op["id"])
    assert interrupted["status"] == "failed"
    assert "restart" in interrupted["error"]


def test_old_history_is_purged(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1"]))
        hub.store.add_history("vpn-1", "ip", {"ip": "8.8.8.8"}, at=utcnow() - timedelta(days=8))
        await hub.check_all()
        return hub.store.history("vpn-1", 100)

    assert all(entry.get("ip") != "8.8.8.8" for entry in asyncio.run(scenario()))
