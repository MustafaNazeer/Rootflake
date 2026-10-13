"""Allowlist CONNECT proxy for the dependency download container.

The download container sits on an internal Docker network whose only other member is this
proxy (docs/adr/0005-local-sandbox-hardening.md, Decision 3). It tunnels CONNECT requests to
allowlisted host names on allowlisted ports and refuses everything else. The name is
resolved here, over IPv4 only, and only public addresses are dialed, so an allowlisted name
that resolves to the LAN, the host, or a metadata address is refused as well.
"""

import asyncio
import ipaddress
import json
import re
import socket
import sys
from dataclasses import dataclass, field

MAX_HEADER_BYTES = 8192
HOST_RE = re.compile(
    r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
)
REQUEST_RE = re.compile(rb"CONNECT ([A-Za-z0-9.-]+):([0-9]{1,5}) HTTP/1\.[01]")

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_connect(head: bytes) -> tuple[str, int]:
    line = head.split(b"\r\n", 1)[0]
    match = REQUEST_RE.fullmatch(line)
    if not match:
        raise ValueError(f"not a plain CONNECT request: {line[:100]!r}")
    host = match.group(1).decode("ascii").lower()
    port = int(match.group(2))
    if not HOST_RE.fullmatch(host) or not 0 < port < 65536:
        raise ValueError(f"bad CONNECT target: {host}:{port}")
    return host, port


@dataclass(frozen=True)
class Allowlist:
    hosts: set[str]
    ports: set[int] = field(default_factory=lambda: {443})

    def __post_init__(self) -> None:
        for host in self.hosts:
            if not HOST_RE.fullmatch(host):
                raise ValueError(f"allowlist entries must be plain lowercase host names: {host!r}")

    def permits(self, host: str, port: int) -> bool:
        return host in self.hosts and port in self.ports


def is_public_address(address: IpAddress) -> bool:
    # IPv6 is refused outright: NAT64 and IPv4 compatible forms can wrap metadata and
    # loopback addresses, and global IPv6 often reaches the laptop and its LAN directly.
    if isinstance(address, ipaddress.IPv6Address):
        return False
    return address.is_global and not address.is_multicast


@dataclass(frozen=True)
class ProxyConfig:
    allowlist: Allowlist
    allow_private: bool = False
    header_timeout_s: float = 10
    idle_timeout_s: float = 120
    max_connections: int = 64


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


async def _reply(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode())
    try:
        await writer.drain()
    finally:
        writer.close()


async def _resolve(host: str, port: int, allow_private: bool) -> str | None:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
    )
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if allow_private or is_public_address(address):
            return str(address)
    return None


@dataclass
class _Activity:
    last: float


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    activity: _Activity,
    idle: float,
) -> bool:
    """Copy one direction until end of stream; False on idle, a stalled peer, or an error.

    The idle clock is shared by both directions, so a long download keeps the tunnel open
    while the client has nothing to send.
    """
    loop = asyncio.get_running_loop()
    try:
        while (wait := activity.last + idle - loop.time()) > 0:
            try:
                chunk = await asyncio.wait_for(reader.read(1 << 16), wait)
            except TimeoutError:
                continue
            if not chunk:
                if writer.can_write_eof():
                    writer.write_eof()
                return True
            writer.write(chunk)
            await asyncio.wait_for(writer.drain(), idle)
            activity.last = loop.time()
    except TimeoutError, OSError:
        pass
    return False


async def _tunnel(
    client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    idle: float,
) -> None:
    activity = _Activity(asyncio.get_running_loop().time())

    async def copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not await _pipe(reader, writer, activity, idle):
            client[1].close()
            upstream[1].close()

    await asyncio.gather(copy(client[0], upstream[1]), copy(upstream[0], client[1]))


async def _handle(
    config: ProxyConfig,
    slots: asyncio.Semaphore,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    if slots.locked():
        await _reply(writer, "503 Service Unavailable")
        return
    async with slots:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), config.header_timeout_s)
        except TimeoutError:
            await _reply(writer, "408 Request Timeout")
            return
        except asyncio.LimitOverrunError:
            await _reply(writer, "431 Request Header Fields Too Large")
            return
        except asyncio.IncompleteReadError, OSError:
            writer.close()
            return
        try:
            host, port = parse_connect(head)
        except ValueError:
            await _reply(writer, "405 Method Not Allowed")
            return
        if not config.allowlist.permits(host, port):
            _log("denied", host=host, port=port, reason="not allowlisted")
            await _reply(writer, "403 Forbidden")
            return
        try:
            address = await asyncio.wait_for(
                _resolve(host, port, config.allow_private), config.header_timeout_s
            )
        except OSError, TimeoutError:
            address = None
        if address is None:
            _log("denied", host=host, port=port, reason="no public address")
            await _reply(writer, "403 Forbidden")
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), config.header_timeout_s
            )
        except OSError, TimeoutError:
            await _reply(writer, "502 Bad Gateway")
            return
        _log("allowed", host=host, port=port, address=address)
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await _tunnel(
                (reader, writer), (upstream_reader, upstream_writer), config.idle_timeout_s
            )
        except OSError:
            pass
        finally:
            upstream_writer.close()
            writer.close()


async def serve(config: ProxyConfig, host: str, port: int) -> asyncio.Server:
    slots = asyncio.Semaphore(config.max_connections)
    return await asyncio.start_server(
        lambda r, w: _handle(config, slots, r, w), host, port, limit=MAX_HEADER_BYTES
    )


def parse_args(argv: list[str]) -> tuple[ProxyConfig, int]:
    import argparse

    parser = argparse.ArgumentParser(description="Allowlist CONNECT proxy")
    parser.add_argument("--port", type=int, default=3128)
    parser.add_argument("--allow", action="append", required=True, help="allowed host name")
    options = parser.parse_args(argv)
    allowlist = Allowlist(hosts={host.lower() for host in options.allow})
    return ProxyConfig(allowlist=allowlist), options.port


async def _main(argv: list[str]) -> None:
    config, port = parse_args(argv)
    server = await serve(config, "0.0.0.0", port)
    _log("listening", port=port, hosts=sorted(config.allowlist.hosts))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
