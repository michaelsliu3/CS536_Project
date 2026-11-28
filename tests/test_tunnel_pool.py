"""Tests for scripts/harness/tunnel_pool.py: the part that shares connections among tunnels.
No SSH connection is opened; local echo servers stand in for the tunnels."""
import asyncio
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tunnel_pool", ROOT / "scripts" / "harness" / "tunnel_pool.py")
tunnel_pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel_pool)


async def echo_server(tag: bytes):
    """A server that answers every message with its tag in front, and the port it listens on."""
    async def answer(reader, writer):
        while data := await reader.read(1024):
            writer.write(tag + data)
            await writer.drain()
        writer.close()
    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class PoolOrder(unittest.TestCase):
    def test_the_least_busy_backend_comes_first(self):
        pool = tunnel_pool.Pool([5, 6, 7])
        self.assertEqual(pool.order(), [5, 6, 7])
        pool.open[5] = 2
        pool.open[6] = 1
        self.assertEqual(pool.order(), [7, 6, 5])


class Handling(unittest.IsolatedAsyncioTestCase):
    async def start(self, ports):
        pool = tunnel_pool.Pool(ports)
        front = await asyncio.start_server(lambda r, w: tunnel_pool.handle(pool, r, w), "127.0.0.1", 0)
        self.addAsyncCleanup(self.close, front)
        return pool, front.sockets[0].getsockname()[1]

    async def close(self, server):
        server.close()
        await server.wait_closed()

    async def backend(self, tag):
        server, port = await echo_server(tag)
        self.addAsyncCleanup(self.close, server)
        return port

    async def test_connections_are_spread_and_bytes_pass_both_ways(self):
        a, b = await self.backend(b"A"), await self.backend(b"B")
        pool, port = await self.start([a, b])
        clients = [await asyncio.open_connection("127.0.0.1", port) for _ in range(4)]
        replies = []
        for reader, writer in clients:
            writer.write(b"hello")
            await writer.drain()
            replies.append(await asyncio.wait_for(reader.read(1024), 5))
        self.assertEqual(sorted(replies), [b"Ahello", b"Ahello", b"Bhello", b"Bhello"])
        self.assertEqual(pool.open, {a: 2, b: 2})
        self.assertEqual((pool.sent, pool.received), (20, 24))
        for _, writer in clients:
            writer.close()
            await writer.wait_closed()
        for _ in range(50):                                       # the counts fall back once the copies end
            if not any(pool.open.values()):
                break
            await asyncio.sleep(0.02)
        self.assertEqual(pool.open, {a: 0, b: 0})

    async def test_a_backend_that_is_down_is_skipped(self):
        up = await self.backend(b"U")
        dead, port_of_dead = await echo_server(b"D")
        await self.close(dead)                                    # nothing listens on this port any more
        pool, port = await self.start([port_of_dead, up])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(1024), 5), b"Ux")
        writer.close()
        await writer.wait_closed()

    async def test_the_connection_is_closed_when_no_backend_is_up(self):
        dead, port_of_dead = await echo_server(b"D")
        await self.close(dead)
        pool, port = await self.start([port_of_dead])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.assertEqual(await asyncio.wait_for(reader.read(1024), 5), b"")
        writer.close()


class Command(unittest.TestCase):
    def test_the_ssh_command_forwards_the_port_with_a_key_only(self):
        command = tunnel_pool.ssh_command("root@host", 2222, 18001, 8000)
        self.assertEqual(command[0], "ssh")
        self.assertIn("BatchMode=yes", command)
        self.assertIn("-C", command)
        self.assertEqual(command[command.index("-L") + 1], "127.0.0.1:18001:localhost:8000")
        self.assertEqual(command[-3:], ["-p", "2222", "root@host"])


if __name__ == "__main__":
    unittest.main()
