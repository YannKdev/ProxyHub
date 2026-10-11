import asyncio
from datetime import timedelta

import pytest
from app.hub import CountryMismatch, ExitBusy, ExitDown, ExitInUse
from app.store import utcnow
from fakes.fake_gluetun import FakeGluetun


def run(coro):
    return asyncio.run(coro)


def test_open_and_close_session(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1"]))
        await hub.check_all()
        session = await hub.open_session("vpn-1")
        opened = hub.describe("vpn-1")["active_sessions"]
        hub.close_session(session["session_id"])
        return hub, session, opened

    hub, session, opened = run(scenario())
    assert session["ip"] == "1.1.1.1"
    assert session["proxy_port"] == 8081
    assert opened == 1
    assert hub.describe("vpn-1")["active_sessions"] == 0
    assert hub.describe("vpn-1")["sessions_since_rotation"] == 1


def test_close_unknown_session(new_hub):
    hub = new_hub(FakeGluetun(["1.1.1.1"]))
    assert hub.close_session("nope") is None


def test_session_refused_during_rotation(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1", "2.2.2.2"]))
        op = hub.start_rotation("vpn-1")
        with pytest.raises(ExitBusy) as error:
            await hub.open_session("vpn-1")
        await hub.wait_operations()
        return op["id"], error.value.operation_id

    op_id, reported = run(scenario())
    assert reported == op_id


def test_session_refused_when_exit_down(new_hub):
    fake = FakeGluetun(["1.1.1.1"])

    async def scenario():
        hub = new_hub(fake)
        fake.reachable = False
        await hub.check_all()
        with pytest.raises(ExitDown):
            await hub.open_session("vpn-1")

    run(scenario())


def test_session_country_must_match(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1"], country="france"))
        session = await hub.open_session("vpn-1", country="France")
        with pytest.raises(CountryMismatch) as error:
            await hub.open_session("vpn-1", country="Belgium")
        return session, error.value.current

    session, current = run(scenario())
    assert session["country"] == "france"
    assert current == "france"


def test_rotation_refused_with_active_sessions(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1", "2.2.2.2"]))
        await hub.open_session("vpn-1")
        with pytest.raises(ExitInUse) as error:
            hub.start_rotation("vpn-1")
        op = hub.start_rotation("vpn-1", force=True)
        await hub.wait_operations()
        return error.value.active_sessions, hub.store.operation(op["id"])

    active, op = run(scenario())
    assert active == 1
    assert op["status"] == "done"


def test_auto_rotation_after_n_sessions(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1", "2.2.2.2"]), rotate_every_sessions=2)
        await hub.check_all()
        first = await hub.open_session("vpn-1")
        hub.close_session(first["session_id"])
        after_one = "vpn-1" in hub.running
        second = await hub.open_session("vpn-1")
        hub.close_session(second["session_id"])
        op_id = hub.running.get("vpn-1")
        await hub.wait_operations()
        return hub, after_one, op_id

    hub, after_one, op_id = run(scenario())
    assert after_one is False
    op = hub.store.operation(op_id)
    assert op["params"]["trigger"] == "auto"
    assert op["result"]["new_ip"] == "2.2.2.2"
    assert hub.describe("vpn-1")["sessions_since_rotation"] == 0


def test_auto_rotation_waits_for_active_sessions(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1", "2.2.2.2"]), rotate_every_sessions=2)
        await hub.check_all()
        sessions = [await hub.open_session("vpn-1") for _ in range(3)]
        hub.close_session(sessions[0]["session_id"])
        hub.close_session(sessions[1]["session_id"])
        while_active = "vpn-1" in hub.running
        hub.close_session(sessions[2]["session_id"])
        after_last = "vpn-1" in hub.running
        await hub.wait_operations()
        return while_active, after_last

    assert run(scenario()) == (False, True)


def test_auto_rotation_waits_for_cooldown(new_hub):
    async def scenario():
        hub = new_hub(
            FakeGluetun(["1.1.1.1", "2.2.2.2"]), rotate_every_sessions=1, cooldown_seconds=60
        )
        await hub.check_all()
        hub.store.save_rotation_time("vpn-1", utcnow() - timedelta(seconds=30))
        session = await hub.open_session("vpn-1")
        hub.close_session(session["session_id"])
        during_cooldown = "vpn-1" in hub.running
        hub.store.save_rotation_time("vpn-1", utcnow() - timedelta(seconds=90))
        hub.sweep_sessions()
        after_cooldown = "vpn-1" in hub.running
        await hub.wait_operations()
        return during_cooldown, after_cooldown

    assert run(scenario()) == (False, True)


def test_expired_sessions_count_for_auto_rotation(new_hub):
    async def scenario():
        hub = new_hub(
            FakeGluetun(["1.1.1.1", "2.2.2.2"]),
            rotate_every_sessions=1,
            session_ttl_seconds=0.01,
        )
        await hub.check_all()
        session = await hub.open_session("vpn-1")
        await asyncio.sleep(0.02)
        hub.sweep_sessions()
        started = "vpn-1" in hub.running
        await hub.wait_operations()
        return hub.store.session(session["session_id"]), started

    session, started = run(scenario())
    assert session["close_reason"] == "expired"
    assert started is True


def test_no_auto_rotation_without_setting(new_hub):
    async def scenario():
        hub = new_hub(FakeGluetun(["1.1.1.1", "2.2.2.2"]))
        for _ in range(5):
            session = await hub.open_session("vpn-1")
            hub.close_session(session["session_id"])
        hub.sweep_sessions()
        return "vpn-1" in hub.running

    assert run(scenario()) is False
