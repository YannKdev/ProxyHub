import asyncio
import time

import pytest
from app.hub import ExitReserved, NoFreeExit, SessionFailed, SessionMoved
from fakes.fake_gluetun import FakeGluetun


def run(coro):
    return asyncio.run(coro)


def two_exits(multi_hub, **settings_overrides):
    fakes = {
        "nord": FakeGluetun(["1.1.1.1", "1.1.1.2", "1.1.1.3"]),
        "cg": FakeGluetun(["2.2.2.1", "2.2.2.2", "2.2.2.3"]),
    }
    hub = multi_hub(
        {"nord": (fakes["nord"], {"pool": "nordvpn"}), "cg": (fakes["cg"], {"pool": "cyberghost"})},
        **settings_overrides,
    )
    return hub, fakes


async def checked(hub):
    await hub.check_all()
    return hub


def test_same_name_same_exit_new_name_other_exit(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        a = await hub.session_for("a", "vpn", None, None)
        again = await hub.session_for("a", "vpn", None, None)
        b = await hub.session_for("b", "vpn", None, None)
        return a, again, b

    a, again, b = run(scenario())
    assert again is a
    assert {a.exit_id, b.exit_id} == {"nord", "cg"}


def test_no_free_exit_is_refused(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        await hub.session_for("a", "vpn", None, None)
        await hub.session_for("b", "vpn", None, None)
        with pytest.raises(NoFreeExit):
            await hub.session_for("c", "vpn", None, None)

    run(scenario())


def test_pool_limits_the_exits(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        session = await hub.session_for("a", "cyberghost", None, None)
        with pytest.raises(NoFreeExit):
            await hub.session_for("b", "cyberghost", None, None)
        return session

    assert run(scenario()).exit_id == "cg"


def test_exit_already_in_country_is_used_as_is(multi_hub):
    async def scenario():
        hub, fakes = two_exits(multi_hub)
        fakes["cg"].countries = ["Belgium"]
        await hub.check_all()
        session = await hub.session_for("a", "vpn", "be", None)
        return hub, session

    hub, session = run(scenario())
    assert session.exit_id == "cg"
    assert hub.running == {}
    assert hub.status["cg"].ip == "2.2.2.1"


def test_country_switch_rotates_a_free_exit(multi_hub):
    async def scenario():
        hub, fakes = two_exits(multi_hub)
        await hub.check_all()
        session = await hub.session_for("a", "nordvpn", "de", None)
        return hub, fakes, session

    hub, fakes, session = run(scenario())
    assert session.exit_id == "nord"
    assert fakes["nord"].countries == ["Germany"]
    assert hub.status["nord"].ip == "1.1.1.2"
    assert hub.status["nord"].country == "Germany"


def test_country_unknown_to_a_provider_tries_the_next_exit(multi_hub):
    async def scenario():
        hub, fakes = two_exits(multi_hub)
        fakes["nord"].known = {"france"}
        await hub.check_all()
        session = await hub.session_for("a", "vpn", "de", None)
        return hub, fakes, session

    hub, fakes, session = run(scenario())
    assert session.exit_id == "cg"
    assert fakes["nord"].countries == ["france"]
    assert "nord" not in hub.reserved


def test_country_unknown_everywhere_fails(multi_hub):
    async def scenario():
        hub, fakes = two_exits(multi_hub)
        for fake in fakes.values():
            fake.known = {"france"}
        await hub.check_all()
        with pytest.raises(SessionFailed, match="not available"):
            await hub.session_for("a", "vpn", "de", None)
        return hub

    hub = run(scenario())
    assert hub.sessions == {}
    assert hub.reserved == {}


def test_unreachable_country_fails_and_frees_the_exit(multi_hub):
    async def scenario():
        hub, fakes = two_exits(multi_hub)
        fakes["nord"].broken_countries = {"germany"}
        await hub.check_all()
        with pytest.raises(SessionFailed, match="switched back"):
            await hub.session_for("a", "nordvpn", "de", None)
        return hub, fakes

    hub, fakes = run(scenario())
    assert hub.reserved == {}
    assert fakes["nord"].countries == ["france"]


def test_down_exit_is_not_given(multi_hub):
    async def scenario():
        hub, fakes = two_exits(multi_hub)
        fakes["nord"].reachable = False
        await hub.check_all()
        session = await hub.session_for("a", "vpn", None, None)
        with pytest.raises(NoFreeExit):
            await hub.session_for("b", "vpn", None, None)
        return session

    assert run(scenario()).exit_id == "cg"


def test_concurrent_requests_share_one_session(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        first, second = await asyncio.gather(
            hub.session_for("a", "nordvpn", "de", None),
            hub.session_for("a", "nordvpn", "de", None),
        )
        return hub, first, second

    hub, first, second = run(scenario())
    assert first is second
    assert len(hub.sessions) == 1


def test_idle_session_is_released_then_exit_rotated(multi_hub):
    async def scenario():
        hub, fakes = two_exits(multi_hub)
        await hub.check_all()
        session = await hub.session_for("a", "nordvpn", None, None)
        hub.sweep()
        kept = session.key in hub.sessions
        session.last_seen = time.monotonic() - 1000
        hub.sweep()
        released = session.key not in hub.sessions and "nord" in hub.stale
        hub.sweep()
        op_id = hub.running["nord"]
        await hub.wait_operations()
        return hub, kept, released, hub.store.operation(op_id)

    hub, kept, released, op = run(scenario())
    assert kept
    assert released
    assert op["params"] == {"country": None, "trigger": "release"}
    assert hub.status["nord"].ip == "1.1.1.2"
    assert "nord" not in hub.stale


def test_session_with_open_connections_is_kept(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        session = await hub.session_for("a", "nordvpn", None, None)
        session.enter()
        session.last_seen = time.monotonic() - 1000
        hub.sweep()
        return hub, session

    hub, session = run(scenario())
    assert session.key in hub.sessions


def test_hold_extends_the_idle_delay(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub, session_idle_seconds=300)[0])
        session = await hub.session_for("a", "nordvpn", None, 60)
        session.last_seen = time.monotonic() - 1000
        hub.sweep()
        return hub, session

    hub, session = run(scenario())
    assert session.idle_seconds == 3600
    assert session.key in hub.sessions


def test_stale_exit_is_rotated_before_reuse(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        first = await hub.session_for("a", "nordvpn", None, None)
        hub.release(first, "test")
        second = await hub.session_for("b", "nordvpn", None, None)
        return hub, second

    hub, second = run(scenario())
    assert second.exit_id == "nord"
    assert hub.status["nord"].ip == "1.1.1.2"
    assert "nord" not in hub.stale


def test_fresh_exit_is_preferred_to_a_stale_one(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        first = await hub.session_for("a", "vpn", None, None)
        first_exit = first.exit_id
        hub.release(first, "test")
        second = await hub.session_for("b", "vpn", None, None)
        return hub, first_exit, second

    hub, first_exit, second = run(scenario())
    assert second.exit_id != first_exit
    assert hub.running == {}


def test_release_by_id_cancels_connections(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        session = await hub.session_for("a", "nordvpn", None, None)

        async def connection():
            session.enter()
            try:
                await asyncio.sleep(10)
            finally:
                session.leave()

        task = asyncio.create_task(connection())
        await asyncio.sleep(0)
        assert hub.release_by_id(session.id)
        assert not hub.release_by_id(session.id)
        await asyncio.gather(task, return_exceptions=True)
        return hub, task

    hub, task = run(scenario())
    assert task.cancelled()
    assert hub.sessions == {}


def test_admin_rotation_of_reserved_exit_needs_force(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        session = await hub.session_for("a", "nordvpn", None, None)
        with pytest.raises(ExitReserved) as error:
            hub.start_rotation("nord")
        hub.start_rotation("nord", force=True)
        await hub.wait_operations()
        return hub, session, error.value

    hub, session, error = run(scenario())
    assert error.session_id == session.id
    assert hub.reserved["nord"] is session
    assert hub.status["nord"].ip == "1.1.1.2"


def test_describe_shows_sessions(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        session = await hub.session_for("a", "cyberghost", None, None)
        return hub, session

    hub, session = run(scenario())
    assert hub.describe("cg")["session_id"] == session.id
    assert hub.describe("nord")["session_id"] is None
    [info] = hub.describe_sessions()
    assert info["label"] == "a"
    assert info["exit"] == "cg"
    assert info["release_after_seconds"] == 300


def test_higher_ip_number_moves_to_a_free_exit(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        first = await hub.session_for("a", "vpn", None, None, 1)
        first_exit = first.exit_id
        again = await hub.session_for("a", "vpn", None, None, 1)
        moved = await hub.session_for("a", "vpn", None, None, 2)
        return hub, first, first_exit, again, moved

    hub, first, first_exit, again, moved = run(scenario())
    assert again is first and moved is first
    assert moved.exit_id != first_exit
    assert moved.ip_number == 2
    assert first_exit in hub.stale
    assert set(hub.reserved) == {moved.exit_id}


def test_ip_number_without_other_free_exit_rotates_the_same_exit(multi_hub):
    async def scenario():
        hub, fakes = two_exits(multi_hub)
        await hub.check_all()
        await hub.session_for("other", "cyberghost", None, None)
        await hub.session_for("a", "nordvpn", None, None, 1)
        moved = await hub.session_for("a", "nordvpn", None, None, 2)
        return hub, moved

    hub, moved = run(scenario())
    assert moved.exit_id == "nord"
    assert hub.status["nord"].ip == "1.1.1.2"
    assert "nord" not in hub.stale


def test_many_ip_changes_never_exhaust_the_exits(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        exits = []
        for n in range(1, 6):
            session = await hub.session_for("a", "vpn", None, None, n)
            exits.append(session.exit_id)
            await hub.wait_operations()
        other = await hub.session_for("b", "vpn", None, None)
        return hub, exits, other

    hub, exits, other = run(scenario())
    assert all(a != b for a, b in zip(exits, exits[1:], strict=False))
    assert len(hub.reserved) == 2
    assert other.exit_id not in (None, exits[-1])


def test_lower_ip_number_is_refused(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        await hub.session_for("a", "vpn", None, None, 3)
        with pytest.raises(SessionMoved) as error:
            await hub.session_for("a", "vpn", None, None, 2)
        same = await hub.session_for("a", "vpn", None, None)
        return error.value, same

    error, same = run(scenario())
    assert error.current == 3
    assert same.ip_number == 3


def test_ip_change_cuts_old_connections(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        session = await hub.session_for("a", "vpn", None, None, 1)

        async def connection():
            session.enter()
            try:
                await asyncio.sleep(10)
            finally:
                session.leave()

        task = asyncio.create_task(connection())
        await asyncio.sleep(0)
        await hub.session_for("a", "vpn", None, None, 2)
        await asyncio.gather(task, return_exceptions=True)
        return task, session

    task, session = run(scenario())
    assert task.cancelled()
    assert session.connections == 0


def test_concurrent_ip_change_moves_once(multi_hub):
    async def scenario():
        hub = await checked(two_exits(multi_hub)[0])
        first = await hub.session_for("a", "vpn", None, None, 1)
        first_exit = first.exit_id
        results = await asyncio.gather(
            hub.session_for("a", "vpn", None, None, 2),
            hub.session_for("a", "vpn", None, None, 2),
        )
        return hub, first_exit, results

    hub, first_exit, (one, two) = run(scenario())
    assert one is two
    assert one.exit_id not in (None, first_exit)
    assert len(hub.reserved) == 1
