#!/usr/bin/env python3
"""Several SSH tunnels to the model server behind one local port.

    .venv/bin/python scripts/harness/tunnel_pool.py -p <ssh port> root@<server>

A single `ssh -L` tunnel carries every Codex session over one TCP connection. On a route that loses
packets, one TCP connection is slow however fast the line is (about 17 KB/s at 15% loss and 270 ms),
and every session waits behind the others' requests. Several connections each get that rate.

This script opens --tunnels compressed SSH tunnels on local ports --first-port, --first-port + 1, ...
and listens on --port (default 8000, the address in configs/codex_harness/config.toml). Each incoming
connection is handed to the tunnel that has the fewest open connections, and a tunnel whose SSH
process ends is started again. It authenticates with a key only (BatchMode) and prints one line a
minute: the open connections per tunnel and the traffic since the last line.
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from datetime import datetime


class Pool:
    """Which backend port gets the next connection: the one with the fewest open connections."""

    def __init__(self, ports: list[int]):
        self.ports = list(ports)
        self.open = {port: 0 for port in self.ports}
        self.sent = self.received = 0

    def order(self) -> list[int]:
        """The backend ports, the least busy first (ties in the given order)."""
        return sorted(self.ports, key=lambda port: (self.open[port], self.ports.index(port)))


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, count) -> None:
    try:
        while data := await reader.read(65536):
            count(len(data))
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def handle(pool: Pool, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str = "127.0.0.1") -> None:
    """Connect one incoming connection to the least busy backend that accepts it, then copy both ways."""
    for port in pool.order():
        try:
            back_reader, back_writer = await asyncio.open_connection(host, port)
        except OSError:
            continue                                              # this tunnel is down: try the next
        pool.open[port] += 1

        def up(n: int) -> None:
            pool.sent += n

        def down(n: int) -> None:
            pool.received += n

        try:
            await asyncio.gather(pipe(reader, back_writer, up), pipe(back_reader, writer, down))
        finally:
            pool.open[port] -= 1
        return
    writer.close()                                                # no tunnel is up


def ssh_command(destination: str, ssh_port: int, local_port: int, remote_port: int) -> list[str]:
    return ["ssh", "-N", "-C", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=20",
            "-L", f"127.0.0.1:{local_port}:localhost:{remote_port}", "-p", str(ssh_port), destination]


async def keep_tunnel(destination: str, ssh_port: int, local_port: int, remote_port: int, delay: float) -> None:
    """Run one SSH tunnel and start it again whenever it ends."""
    await asyncio.sleep(delay)                                    # do not open every connection in the same instant
    while True:
        started = time.time()
        process = await asyncio.create_subprocess_exec(*ssh_command(destination, ssh_port, local_port, remote_port),
                                                       stdin=subprocess.DEVNULL)
        code = await process.wait()
        print(f"{datetime.now():%H:%M:%S} the tunnel on port {local_port} ended (exit {code}); starting it again", flush=True)
        await asyncio.sleep(5 if time.time() - started > 60 else 30)


async def report(pool: Pool, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        sent, received, pool.sent, pool.received = pool.sent, pool.received, 0, 0
        print(f"{datetime.now():%H:%M:%S} open connections per tunnel {list(pool.open.values())}; "
              f"{sent / 1024 / every:.0f} KB/s to the server, {received / 1024 / every:.0f} KB/s back", flush=True)


async def run(args) -> None:
    ports = [args.first_port + i for i in range(args.tunnels)]
    pool = Pool(ports)
    tunnels = [asyncio.create_task(keep_tunnel(args.destination, args.ssh_port, port, args.remote_port, 0.7 * i))
               for i, port in enumerate(ports)]
    server = await asyncio.start_server(lambda r, w: handle(pool, r, w), "127.0.0.1", args.port)
    print(f"listening on 127.0.0.1:{args.port}; {args.tunnels} compressed SSH tunnels to {args.destination} "
          f"(local ports {ports[0]} to {ports[-1]}, the server's port {args.remote_port})", flush=True)
    async with server:
        await asyncio.gather(server.serve_forever(), report(pool, args.report), *tunnels)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("destination", help="the SSH destination, for example root@host")
    parser.add_argument("-p", "--ssh-port", type=int, default=22, help="the SSH port (default 22)")
    parser.add_argument("--tunnels", type=int, default=6, help="SSH connections to open (default 6)")
    parser.add_argument("--port", type=int, default=8000, help="the local port to listen on (default 8000)")
    parser.add_argument("--first-port", type=int, default=18001, help="the first local port of the tunnels (default 18001)")
    parser.add_argument("--remote-port", type=int, default=8000, help="the model server's port on the server (default 8000)")
    parser.add_argument("--report", type=float, default=60, help="seconds between status lines (default 60)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
