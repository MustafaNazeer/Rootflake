import asyncio
import ipaddress
import socket
import struct
import time

import pytest

from rootflake.sandbox import proxy as proxy_module
from rootflake.sandbox.proxy import (
    Allowlist,
    ProxyConfig,
    is_public_address,
    parse_args,
    parse_connect,
    serve,
)


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (
            b"CONNECT repo.maven.apache.org:443 HTTP/1.1\r\nHost: x\r\n\r\n",
            ("repo.maven.apache.org", 443),
        ),
        (b"CONNECT Repo1.Maven.ORG:443 HTTP/1.0\r\n\r\n", ("repo1.maven.org", 443)),
    ],
)
def test_parse_connect_accepts_a_plain_connect(head, expected):
    assert parse_connect(head) == expected


@pytest.mark.parametrize(
    "head",
    [
        b"GET http://repo1.maven.org/ HTTP/1.1\r\n\r\n",
        b"CONNECT repo1.maven.org HTTP/1.1\r\n\r\n",
        b"CONNECT repo1.maven.org:abc HTTP/1.1\r\n\r\n",
        b"CONNECT repo1.maven.org:443:1 HTTP/1.1\r\n\r\n",
        b"CONNECT user@repo1.maven.org:443 HTTP/1.1\r\n\r\n",
        b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
        b"CONNECT repo1.maven.org:443 SPDY/3\r\n\r\n",
        b"CONNECT  repo1.maven.org:443 HTTP/1.1\r\n\r\n",
        b"\xff\xfe\r\n\r\n",
    ],
)
def test_parse_connect_rejects_everything_else(head):
    with pytest.raises(ValueError):
        parse_connect(head)


def test_allowlist_matches_exact_hosts_and_ports_only():
    allow = Allowlist(hosts={"repo1.maven.org", "repo.maven.apache.org"}, ports={443})
    assert allow.permits("repo1.maven.org", 443)
    assert not allow.permits("repo1.maven.org", 80)
    assert not allow.permits("evil.repo1.maven.org", 443)
    assert not allow.permits("repo1.maven.org.evil.com", 443)


def test_allowlist_rejects_malformed_entries():
    with pytest.raises(ValueError):
        Allowlist(hosts={"*.maven.org"}, ports={443})
    with pytest.raises(ValueError):
        Allowlist(hosts={"a b"}, ports={443})


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("151.101.0.215", True),
        ("2a04:4e42::215", False),
        ("64:ff9b::a9fe:a9fe", False),
        ("::127.0.0.1", False),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.17.0.1", False),
        ("192.168.1.10", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("0.0.0.0", False),
        ("::ffff:127.0.0.1", False),
        ("224.0.0.1", False),
        ("233.252.0.1", False),
    ],
)
def test_only_public_addresses_are_dialed(address, public):
    assert is_public_address(ipaddress.ip_address(address)) is public


async def _echo_server():
    async def handle(reader, writer):
        data = await reader.read(100)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def _through_proxy(proxy_port, head, payload=b""):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(head)
    await writer.drain()
    status = await reader.readline()
    while (await reader.readline()) not in (b"\r\n", b""):
        pass
    body = b""
    if status.startswith(b"HTTP/1.1 200"):
        writer.write(payload)
        await writer.drain()
        body = await asyncio.wait_for(reader.read(100), 5)
    writer.close()
    return status, body


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def _config(port, **overrides):
    values = dict(
        allowlist=Allowlist(hosts={"localhost"}, ports={port}),
        allow_private=True,
        header_timeout_s=2,
        idle_timeout_s=5,
        max_connections=8,
    )
    values.update(overrides)
    return ProxyConfig(**values)


def test_allowed_connect_is_tunneled():
    async def scenario():
        echo = await _echo_server()
        port = echo.sockets[0].getsockname()[1]
        proxy = await serve(_config(port), "127.0.0.1", 0)
        proxy_port = proxy.sockets[0].getsockname()[1]
        head = f"CONNECT localhost:{port} HTTP/1.1\r\n\r\n".encode()
        result = await _through_proxy(proxy_port, head, b"hi")
        proxy.close()
        echo.close()
        return result

    status, body = _run(scenario())
    assert status.startswith(b"HTTP/1.1 200")
    assert body == b"echo:hi"


def test_host_outside_the_allowlist_is_refused():
    async def scenario():
        proxy = await serve(_config(443), "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]
        result = await _through_proxy(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        proxy.close()
        return result

    status, _ = _run(scenario())
    assert status.startswith(b"HTTP/1.1 403")


def test_private_address_is_refused_even_when_the_name_is_allowed():
    async def scenario():
        echo = await _echo_server()
        port = echo.sockets[0].getsockname()[1]
        proxy = await serve(_config(port, allow_private=False), "127.0.0.1", 0)
        proxy_port = proxy.sockets[0].getsockname()[1]
        head = f"CONNECT localhost:{port} HTTP/1.1\r\n\r\n".encode()
        result = await _through_proxy(proxy_port, head)
        proxy.close()
        echo.close()
        return result

    status, _ = _run(scenario())
    assert status.startswith(b"HTTP/1.1 403")


def test_plain_http_request_is_refused():
    async def scenario():
        proxy = await serve(_config(443), "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]
        result = await _through_proxy(port, b"GET http://repo1.maven.org/ HTTP/1.1\r\n\r\n")
        proxy.close()
        return result

    status, _ = _run(scenario())
    assert status.startswith(b"HTTP/1.1 405")


def test_oversized_header_is_refused():
    async def scenario():
        proxy = await serve(_config(443), "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]
        head = b"CONNECT localhost:443 HTTP/1.1\r\nX: " + b"a" * 20000 + b"\r\n\r\n"
        result = await _through_proxy(port, head)
        proxy.close()
        return result

    status, _ = _run(scenario())
    assert status.startswith(b"HTTP/1.1 431")


def test_silent_client_is_dropped_after_the_header_timeout():
    async def scenario():
        proxy = await serve(_config(443, header_timeout_s=0.3), "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        data = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        proxy.close()
        return data

    assert _run(scenario()).startswith(b"HTTP/1.1 408")


def test_command_line_builds_the_config():
    config, port = parse_args(
        ["--port", "3128", "--allow", "repo1.maven.org", "--allow", "Repo.Maven.Apache.org"]
    )
    assert port == 3128
    assert config.allowlist.hosts == {"repo1.maven.org", "repo.maven.apache.org"}
    assert config.allowlist.ports == {443}
    assert config.allow_private is False


def test_command_line_requires_at_least_one_host():
    with pytest.raises(SystemExit):
        parse_args(["--port", "3128"])


def test_resolution_asks_for_ipv4_only(monkeypatch):
    seen = {}

    async def fake_getaddrinfo(host, port, **kwargs):
        seen.update(kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.0.215", port))]

    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        return await proxy_module._resolve("repo1.maven.org", 443, False)

    assert asyncio.run(scenario()) == "151.101.0.215"
    assert seen["family"] == socket.AF_INET


async def _open_tunnel(proxy_port, upstream_port, extra=b""):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(f"CONNECT localhost:{upstream_port} HTTP/1.1\r\n\r\n".encode() + extra)
    await writer.drain()
    status = await asyncio.wait_for(reader.readline(), 5)
    while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b""):
        pass
    return status, reader, writer


async def _read_all(reader, timeout):
    data = b""
    while chunk := await asyncio.wait_for(reader.read(1024), timeout):
        data += chunk
    return data


def test_a_download_trickling_past_the_idle_timeout_reaches_a_silent_client():
    async def trickle(reader, writer):
        for _ in range(12):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.1)
        writer.close()

    async def scenario():
        upstream = await asyncio.start_server(trickle, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]
        proxy = await serve(_config(port, idle_timeout_s=0.4), "127.0.0.1", 0)
        status, reader, writer = await _open_tunnel(proxy.sockets[0].getsockname()[1], port)
        body = await _read_all(reader, 5)
        writer.close()
        proxy.close()
        upstream.close()
        return status, body

    status, body = _run(scenario())
    assert status.startswith(b"HTTP/1.1 200")
    assert body == b"x" * 12


def test_a_tunnel_silent_in_both_directions_is_closed_after_the_idle_timeout():
    async def hold(reader, writer):
        await reader.read()
        writer.close()

    async def scenario():
        upstream = await asyncio.start_server(hold, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]
        proxy = await serve(_config(port, idle_timeout_s=0.3), "127.0.0.1", 0)
        _, reader, writer = await _open_tunnel(proxy.sockets[0].getsockname()[1], port)
        started = time.monotonic()
        body = await _read_all(reader, 5)
        elapsed = time.monotonic() - started
        writer.close()
        proxy.close()
        upstream.close()
        return body, elapsed

    body, elapsed = _run(scenario())
    assert body == b""
    assert elapsed < 3


def test_a_half_close_from_the_client_still_lets_the_reply_through():
    async def reply_after_eof(reader, writer):
        data = await reader.read()
        writer.write(b"got:" + data)
        await writer.drain()
        writer.close()

    async def scenario():
        upstream = await asyncio.start_server(reply_after_eof, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]
        proxy = await serve(_config(port), "127.0.0.1", 0)
        _, reader, writer = await _open_tunnel(proxy.sockets[0].getsockname()[1], port)
        writer.write(b"request")
        await writer.drain()
        writer.write_eof()
        body = await _read_all(reader, 5)
        writer.close()
        proxy.close()
        upstream.close()
        return body

    assert _run(scenario()) == b"got:request"


def test_payload_sent_with_the_connect_header_is_forwarded():
    async def scenario():
        echo = await _echo_server()
        port = echo.sockets[0].getsockname()[1]
        proxy = await serve(_config(port), "127.0.0.1", 0)
        _, reader, writer = await _open_tunnel(proxy.sockets[0].getsockname()[1], port, b"early")
        body = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        proxy.close()
        echo.close()
        return body

    assert _run(scenario()) == b"echo:early"


def test_a_client_reset_before_the_tunnel_starts_closes_the_upstream_socket(monkeypatch):
    async def slow_resolve(host, port, allow_private):
        await asyncio.sleep(0.3)
        return "127.0.0.1"

    monkeypatch.setattr(proxy_module, "_resolve", slow_resolve)

    async def scenario():
        closed = asyncio.Event()

        async def watch(reader, writer):
            await reader.read()
            closed.set()
            writer.close()

        upstream = await asyncio.start_server(watch, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]
        proxy = await serve(_config(port, idle_timeout_s=30), "127.0.0.1", 0)
        _, writer = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
        writer.write(f"CONNECT localhost:{port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        writer.transport.abort()
        await asyncio.wait_for(closed.wait(), 5)
        proxy.close()
        upstream.close()

    _run(scenario())


def test_a_hanging_name_lookup_is_refused_after_the_header_timeout(monkeypatch):
    async def scenario():
        async def never(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", never)
        proxy = await serve(_config(443, header_timeout_s=0.3), "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", proxy.sockets[0].getsockname()[1]
        )
        writer.write(b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 3)
        writer.close()
        proxy.close()
        return status

    assert _run(scenario()).startswith(b"HTTP/1.1 403")


def test_connections_beyond_the_limit_are_refused_at_once():
    async def scenario():
        proxy = await serve(_config(443, max_connections=1, header_timeout_s=5), "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]
        _, first = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.2)
        reader, second = await asyncio.open_connection("127.0.0.1", port)
        status = await asyncio.wait_for(reader.readline(), 2)
        first.close()
        second.close()
        proxy.close()
        return status

    assert _run(scenario()).startswith(b"HTTP/1.1 503")


class _BrokenReader:
    async def read(self, n):
        raise OSError(113, "No route to host")


class _NullWriter:
    def __init__(self):
        self.closed = False

    def write(self, data):
        pass

    async def drain(self):
        pass

    def can_write_eof(self):
        return True

    def write_eof(self):
        pass


def test_an_os_error_while_copying_ends_the_pipe_instead_of_escaping():
    async def scenario():
        activity = proxy_module._Activity(asyncio.get_running_loop().time())
        return await proxy_module._pipe(_BrokenReader(), _NullWriter(), activity, 5)

    assert _run(scenario()) is False
