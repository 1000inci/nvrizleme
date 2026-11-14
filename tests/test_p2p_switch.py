"""Sayfa değiştirme senaryosu: sürekli veri akan 4 bağlantı art arda açılıp kapanır.

Gerçek kullanımda görülen "ikinci sayfa değişiminden sonra yeni akış açılmıyor"
hatasını sahte cihazla yakalamak için.
"""
import asyncio
import os
import struct

from nvrizleme.p2p import MuxPtcp, P2PSession, RawBody  # önce: yamayı kurar
from opendahua.object.address import Address
from opendahua.udp.udp_socket import UdpSocket

CHUNK = os.urandom(900)


async def stream_handler(reader, writer):
    """RTSP kamerası gibi: istemci bir şey yollayınca kesintisiz veri basar."""
    try:
        await reader.read(100)
        while True:
            writer.write(CHUNK)
            await writer.drain()
            await asyncio.sleep(0.002)
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


class FakeDevice:
    def __init__(self, ptcp, port):
        self.ptcp, self.port, self.conns = ptcp, port, {}
        ptcp.on_control = self.control

    def control(self, raw):
        rid = struct.unpack("!L", raw[4:8])[0]
        if raw[0] == 0x11:
            self.ptcp.adopt_realm(rid)
            self.conns[rid] = asyncio.create_task(self.serve(rid))
            self.ptcp._queue_send.put_nowait(
                RawBody(struct.pack("!B3xLL", 0x12, rid, 0) + b"CONN"))
        elif raw[0] == 0x12 and b"DISC" in raw:
            task = self.conns.pop(rid, None)
            if task:
                task.cancel()
            self.ptcp._realms.pop(rid, None)
            self.ptcp._queue_send.put_nowait(
                RawBody(struct.pack("!B3xLL", 0x12, rid, 0) + b"DISC"))

    async def serve(self, rid):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)

        async def up():
            while (data := await self.ptcp.recv_realm(rid)) is not None:
                writer.write(data)
                await writer.drain()

        t = asyncio.create_task(up())
        try:
            while data := await reader.read(65536):
                self.ptcp.send_realm(rid, data)
                while self.ptcp.pending > 256:
                    await asyncio.sleep(0.005)
        finally:
            t.cancel()
            writer.close()


async def scenario(rounds=5, per_round=4):
    srv = await asyncio.start_server(stream_handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    a = await UdpSocket.create(Address.create_from_ip_and_port("127.0.0.1", 1))
    b = await UdpSocket.create(Address.create_from_ip_and_port("127.0.0.1", 1))
    a.set_address_remote(Address.create_from_ip_and_port(
        "127.0.0.1", b.get_address_local().get_port()))
    b.set_address_remote(Address.create_from_ip_and_port(
        "127.0.0.1", a.get_address_local().get_port()))
    logs = []
    dev = MuxPtcp(b, logs.append)
    FakeDevice(dev, port)
    await dev.connect()
    s = P2PSession("TEST", "admin", "x", logs.append)
    s.ptcp = MuxPtcp(a, logs.append)
    await s.ptcp.connect()
    lport = await s.forward(554)

    async def view(seconds):
        r, w = await asyncio.open_connection("127.0.0.1", lport)
        w.write(b"PLAY")
        got = 0
        end = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < end:
            got += len(await asyncio.wait_for(r.read(65536), 5))
        w.close()
        return got

    for n in range(rounds):
        results = await asyncio.gather(*(view(1.5) for _ in range(per_round)))
        assert all(g > 50_000 for g in results), (n, results, logs[-15:])
        await asyncio.sleep(0.3)
    errors = [l for l in logs if "hatası" in l]
    assert not errors, errors
    await s.close()
    await dev.disconnect()
    srv.close()


def test_page_switching():
    asyncio.run(asyncio.wait_for(scenario(), 90))
