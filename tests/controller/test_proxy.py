import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from app.guard import Guard
from app.proxy import EntryProxy, ProxyError, parse_head, parse_username
from fakes.fake_gluetun import FakeGluetun
from fakes.fake_upstream import FakeUpstream


def run(coro):
    return asyncio.run(coro)


def basic(user: str, password: str = "password") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("user", {"session": None, "pool": "vpn", "country": None, "hold": None, "ip": None}),
        (
            "user-session-a1",
            {"session": "a1", "pool": "vpn", "country": None, "hold": None, "ip": None},
        ),
        (
            "user-pool-CyberGhost-country-BE-session-Job_7-ip-4-hold-30",
            {"session": "Job_7", "pool": "cyberghost", "country": "be", "hold": 30, "ip": 4},
        ),
    ],
)
def test_username_options(username, expected):
    assert vars(parse_username(username, "user")) == expected


@pytest.mark.parametrize("username", ["other", "users-session-a", "use"])
def test_other_user_is_not_ours(username):
    assert parse_username(username, "user") is None


@pytest.mark.parametrize(
    "username",
    [
        "user-session",
        "user-color-red",
        "user-session-a-session-b",
        "user-country-zz",
        "user-country-fra",
        "user-hold-0",
        "user-hold-x",
        "user-ip-0",
        "user-session-a-ip-x",
        "user-session-a.b",
        "user-",
    ],
)
def test_bad_options_are_refused(username):
    with pytest.raises(ProxyError) as error:
        parse_username(username, "user")
    assert error.value.status == 400


def test_parse_connect_and_absolute_url():
    connect = parse_head(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    assert (connect.method, connect.host, connect.port) == ("CONNECT", "example.com", 443)
    get = parse_head(b"GET http://example.com/a?b=1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
    assert (get.host, get.port, get.header("host")) == ("example.com", 80, "example.com")


@pytest.mark.parametrize(
    "head",
    [
        b"GET /relative HTTP/1.1\r\n\r\n",
        b"GET https://example.com/ HTTP/1.1\r\n\r\n",
        b"CONNECT example.com HTTP/1.1\r\n\r\n",
        b"NONSENSE\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nno colon\r\n\r\n",
    ],
)
def test_bad_requests_are_refused(head):
    with pytest.raises(ProxyError) as error:
        parse_head(head)
    assert error.value.status == 400


@asynccontextmanager
async def running_proxy(multi_hub, **settings_overrides):
    upstreams = {name: await FakeUpstream(name).start() for name in ("nord", "cg")}
    hub = multi_hub(
        {
            "nord": (FakeGluetun(["1.1.1.1", "1.1.1.2"]), {"proxy_url": upstreams["nord"].url}),
            "cg": (
                FakeGluetun(["2.2.2.1", "2.2.2.2"]),
                {"pool": "cyberghost", "proxy_url": upstreams["cg"].url},
            ),
        },
        **settings_overrides,
    )
    await hub.check_all()
    guard = Guard(3, 600, 1800)
    proxy = EntryProxy(hub, hub.settings, guard)
    port = await proxy.start("127.0.0.1", 0)
    try:
        yield SimpleNamespace(hub=hub, port=port, upstreams=upstreams, guard=guard)
    finally:
        await proxy.close()
        for upstream in upstreams.values():
            await upstream.close()
        await hub.close()


async def connect(env, user: str | None, target: str = "example.com:443", password="password"):
    """CONNECT par le proxy. Renvoie (code, sortie vue, reader, writer)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", env.port)
    head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if user is not None:
        head += f"Proxy-Authorization: {basic(user, password)}\r\n"
    writer.write((head + "\r\n").encode())
    await writer.drain()
    reply = await reader.readuntil(b"\r\n\r\n")
    status = int(reply.split(b" ")[1])
    exit_name = None
    if status == 200:
        exit_name = (await reader.readline()).decode().strip().removeprefix("exit=")
    return status, exit_name, reader, writer, reply


async def connect_status(env, user, target="example.com:443", password="password"):
    status, exit_name, _, writer, _ = await connect(env, user, target, password)
    writer.close()
    return status, exit_name


def test_connect_tunnel_relays_both_ways(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            status, exit_name, reader, writer, _ = await connect(env, "user-session-a")
            writer.write(b"ping")
            await writer.drain()
            echo = await reader.readexactly(4)
            writer.close()
            return status, exit_name, echo, env.upstreams[exit_name].heads[0]

    status, exit_name, echo, upstream_head = run(scenario())
    assert status == 200
    assert exit_name in ("nord", "cg")
    assert echo == b"ping"
    assert upstream_head.startswith("CONNECT example.com:443 HTTP/1.1\r\n")
    assert basic("user") in upstream_head


def test_plain_http_is_forwarded_without_client_credentials(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            reader, writer = await asyncio.open_connection("127.0.0.1", env.port)
            writer.write(
                b"GET http://example.com/page HTTP/1.1\r\nHost: example.com\r\n"
                b"Proxy-Authorization: " + basic("user-session-a").encode() + b"\r\n"
                b"Proxy-Connection: keep-alive\r\n\r\n"
            )
            await writer.drain()
            response = await reader.read()
            writer.close()
            heads = [h for u in env.upstreams.values() for h in u.heads]
            return response, heads

    response, [upstream_head] = run(scenario())
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"GET http://example.com/page HTTP/1.1" in response
    assert basic("user-session-a") not in upstream_head
    assert basic("user") in upstream_head
    assert "Connection: close" in upstream_head
    assert "Proxy-Connection" not in upstream_head


def test_sessions_pick_exits(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            a1 = await connect_status(env, "user-session-a")
            a2 = await connect_status(env, "user-session-a")
            b = await connect_status(env, "user-session-b")
            c = await connect_status(env, "user-session-c")
            cg = await connect_status(env, "user-pool-cyberghost-session-d")
            return a1, a2, b, c, cg

    a1, a2, b, c, cg = run(scenario())
    assert a1 == a2
    assert {a1[1], b[1]} == {"nord", "cg"}
    assert c == (503, None)
    assert cg == (503, None)


def test_without_session_one_session_per_client_ip(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            first = await connect_status(env, "user")
            second = await connect_status(env, "user")
            return first, second, env.hub.describe_sessions()

    first, second, sessions = run(scenario())
    assert first == second
    assert [s["label"] for s in sessions] == ["ip:127.0.0.1"]


def test_country_option_switches_the_exit(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            status = await connect_status(env, "user-pool-cyberghost-country-de-session-x")
            return status, env.hub.status["cg"].country

    assert run(scenario()) == ((200, "cg"), "Germany")


def test_authentication(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            _, _, _, writer, missing = await connect(env, None)
            writer.close()
            wrong = [await connect_status(env, "user", password="nope") for _ in range(3)]
            blocked = await connect_status(env, "user")
            other = await connect_status(env, "admin")
            return missing, wrong, blocked, other, env.hub.sessions

    missing, wrong, blocked, other, sessions = run(scenario())
    assert missing.startswith(b"HTTP/1.1 407")
    assert b'Proxy-Authenticate: Basic realm="proxyhub"' in missing
    assert wrong == [(407, None)] * 3
    assert blocked == (429, None)
    assert other == (429, None)
    assert sessions == {}


def test_missing_credentials_do_not_count_as_failures(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            for _ in range(5):
                await connect_status(env, None)
            return await connect_status(env, "user")

    assert run(scenario())[0] == 200


@pytest.mark.parametrize(
    ("user", "target", "status"),
    [
        ("user-colour-red", "example.com:443", 400),
        ("user-pool-mobile", "example.com:443", 400),
        ("user", "example.com:25", 403),
        ("user", "10.0.0.1:443", 403),
        ("user", "127.0.0.1:443", 403),
        ("user", "[::1]:443", 403),
        ("user", "localhost:443", 403),
    ],
)
def test_refused_requests(multi_hub, user, target, status):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            return await connect_status(env, user, target), env.hub.sessions

    assert run(scenario()) == ((status, None), {})


def test_exit_failing_to_reach_the_host_gives_502(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            for upstream in env.upstreams.values():
                upstream.connect_status = 503
            status, _, _, writer, reply = await connect(env, "user-session-a")
            writer.close()
            for upstream in env.upstreams.values():
                upstream.connect_status = 407
            auth = await connect_status(env, "user-session-a")
            return status, reply, auth

    status, reply, auth = run(scenario())
    # Jamais 503 : il veut dire « plus de sortie libre » pour le client.
    assert status == 502
    assert reply.startswith(b"HTTP/1.1 502 exit could not connect to example.com (answered 503)")
    assert auth == (502, None)


def test_unreachable_exit_gives_502(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            for upstream in env.upstreams.values():
                await upstream.close()
                upstream._server.close()
                await upstream._server.wait_closed()
            return await connect_status(env, "user-session-a")

    assert run(scenario()) == (502, None)


def test_connection_counts_in_session(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            _, _, _, writer, _ = await connect(env, "user-session-a")
            await asyncio.sleep(0.05)
            during = env.hub.describe_sessions()[0]["connections"]
            writer.close()
            await asyncio.sleep(0.05)
            after = env.hub.describe_sessions()[0]["connections"]
            return during, after

    assert run(scenario()) == (1, 0)


def test_plain_http_connection_ends_with_the_response(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            reader, writer = await asyncio.open_connection("127.0.0.1", env.port)
            writer.write(
                b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n"
                b"Proxy-Authorization: " + basic("user-session-a").encode() + b"\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 5)
            await asyncio.sleep(0.05)
            connections = env.hub.describe_sessions()[0]["connections"]
            writer.close()
            return response, connections

    response, connections = run(scenario())
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert connections == 0


def test_ip_option_changes_the_exit(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            first = await connect_status(env, "user-session-a-ip-1")
            again = await connect_status(env, "user-session-a-ip-1")
            moved = await connect_status(env, "user-session-a-ip-2")
            back = await connect_status(env, "user-session-a-ip-1")
            return first, again, moved, back, len(env.hub.reserved)

    first, again, moved, back, reserved = run(scenario())
    assert first == again
    assert moved[0] == 200 and moved[1] != first[1]
    assert back == (409, None)
    assert reserved == 1


def test_first_connection_moves_to_another_exit_when_the_exit_fails(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            await env.upstreams["nord"].close()
            status = await connect_status(env, "user-session-a")
            [session] = env.hub.describe_sessions()
            return status, session["exit"], "nord" in env.hub.reserved

    status, exit_id, nord_reserved = run(scenario())
    assert status == (200, "cg")
    assert exit_id == "cg"
    assert not nord_reserved


def test_exit_failing_after_a_relay_keeps_the_session(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            first = await connect_status(env, "user-session-a")
            await env.upstreams[first[1]].close()
            status, _, _, writer, reply = await connect(env, "user-session-a")
            writer.close()
            return first, status, reply, env.hub.describe_sessions()[0]["exit"]

    first, status, reply, exit_id = run(scenario())
    assert status == 502
    assert reply.startswith(b"HTTP/1.1 502 exit unreachable")
    assert exit_id == first[1]


def test_status_line_carries_the_message(multi_hub):
    async def scenario():
        async with running_proxy(multi_hub) as env:
            _, _, _, writer, bad_option = await connect(env, "user-colour-red")
            writer.close()
            await connect_status(env, "user-session-a-ip-2")
            _, _, _, writer, moved = await connect(env, "user-session-a-ip-1")
            writer.close()
            return bad_option, moved

    bad_option, moved = run(scenario())
    assert bad_option.startswith(b"HTTP/1.1 400 unknown proxy option 'colour'\r\n")
    assert moved.startswith(b"HTTP/1.1 409 session already moved to ip 2, use -ip-2 or higher")
