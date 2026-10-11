import asyncio
import time

from fakes.fake_gluetun import FakeGluetun


def run(coro):
    return asyncio.run(coro)


async def down_hub(new_hub, fake, down_for=1000, **overrides):
    """Sortie vue `down` depuis `down_for` secondes."""
    hub = new_hub(fake, **overrides)
    await hub.check_all()
    fake.status = "stopped"
    await hub.check_all()
    hub.status["vpn-1"].down_since = time.monotonic() - down_for
    return hub


def test_down_exit_is_recovered(new_hub):
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"])

    async def scenario():
        hub = await down_hub(new_hub, fake)
        hub.sweep()
        op_id = hub.running["vpn-1"]
        state_during = hub.describe("vpn-1")["state"]
        await hub.wait_operations()
        return hub, hub.store.operation(op_id), state_during

    hub, op, state_during = run(scenario())
    assert state_during == "recovering"
    assert op["type"] == "recover"
    assert op["status"] == "done"
    assert op["result"]["new_ip"] == "2.2.2.2"
    assert hub.status["vpn-1"].state == "up"
    assert hub.status["vpn-1"].down_since is None
    events = [e["event"] for e in hub.store.history("vpn-1", 10)]
    assert "recovery" in events
    assert events.count("state") == 2


def test_recovery_accepts_same_ip(new_hub):
    fake = FakeGluetun(["1.1.1.1"])

    async def scenario():
        hub = await down_hub(new_hub, fake)
        hub.sweep()
        op_id = hub.running["vpn-1"]
        await hub.wait_operations()
        return hub.store.operation(op_id)

    op = run(scenario())
    assert op["status"] == "done"
    assert op["result"]["new_ip"] == "1.1.1.1"


def test_no_recovery_before_delay(new_hub):
    async def scenario():
        hub = await down_hub(new_hub, FakeGluetun(["1.1.1.1"]), down_for=10)
        hub.sweep()
        return "vpn-1" in hub.running

    assert run(scenario()) is False


def test_recovery_attempts_are_spaced(new_hub):
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"])

    async def scenario():
        hub = await down_hub(new_hub, fake)
        fake.connects = False
        hub.sweep()
        op_id = hub.running["vpn-1"]
        await hub.wait_operations()
        hub.sweep()
        retried_at_once = "vpn-1" in hub.running
        hub.status["vpn-1"].last_recovery = time.monotonic() - 1000
        hub.sweep()
        retried_later = "vpn-1" in hub.running
        await hub.wait_operations()
        return hub.store.operation(op_id), retried_at_once, retried_later

    op, retried_at_once, retried_later = run(scenario())
    assert op["status"] == "failed"
    assert retried_at_once is False
    assert retried_later is True


def test_no_recovery_when_disabled(new_hub):
    async def scenario():
        hub = await down_hub(new_hub, FakeGluetun(["1.1.1.1"]), recover_after_seconds=None)
        hub.sweep()
        return "vpn-1" in hub.running

    assert run(scenario()) is False


def test_recovery_ignores_active_sessions(new_hub):
    fake = FakeGluetun(["1.1.1.1", "2.2.2.2"])

    async def scenario():
        hub = new_hub(fake)
        await hub.check_all()
        await hub.open_session("vpn-1")
        fake.status = "stopped"
        await hub.check_all()
        hub.status["vpn-1"].down_since = time.monotonic() - 1000
        hub.sweep()
        started = "vpn-1" in hub.running
        await hub.wait_operations()
        return started

    assert run(scenario()) is True


def test_recovery_does_not_touch_rotation_cooldown(new_hub):
    async def scenario():
        hub = await down_hub(new_hub, FakeGluetun(["1.1.1.1", "2.2.2.2"]))
        hub.sweep()
        await hub.wait_operations()
        return hub.describe("vpn-1")["last_rotation_at"]

    assert run(scenario()) is None
