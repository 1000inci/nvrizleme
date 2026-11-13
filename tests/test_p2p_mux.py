"""P2P çoklayıcı, tünel HTTP'si ve port yönlendirme testi.

Gerçek NVR yerine, aynı PTCP kodunu kullanan sahte bir cihaz yerel UDP
üzerinden karşı taraf olur. Sinyalleşme (Easy4IP) burada test EDİLMEZ.
"""
import asyncio
import hashlib
import os
import struct

from opendahua.object.address import Address
from opendahua.udp.udp_socket import UdpSocket

from nvrizleme.p2p import MuxPtcp, P2PSession, RawBody

USER, PASSWORD, REALM, NONCE = "admin", "s@fre#1:x", "Login to TEST", "abc123"
BODY = "table.ChannelTitle[0].Name=Kapi\r\ntable.ChannelTitle[1].Name=Otopark\r\n"


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


async def http_handler(reader, writer):
    head = (await reader.readuntil(b"\r\n\r\n")).decode()
    path = head.split()[1]
    expected = md5(f"{md5(f'{USER}:{REALM}:{PASSWORD}')}:{NONCE}:{md5(f'GET:{path}')}")
    if f'response="{expected}"' in head:
        body = BODY.encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    else:
        writer.write(b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Digest "
                     b'realm="%s", nonce="%s"\r\nContent-Length: 0\r\n\r\n'
                     % (REALM.encode(), NONCE.encode()))
    await writer.drain()
    writer.close()


async def echo_handler(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    writer.close()


class FakeDevice:
    def __init__(self, ptcp, ports):
        self.ptcp, self.ports, self.tasks = ptcp, ports, []
        ptcp.on_control = self.control

    def control(self, raw):
        if raw[0] == 0x11:
            _, rid, _, port, _ = struct.unpack("!B3xLLLL", raw)
            self.ptcp.adopt_realm(rid)
            self.tasks.append(asyncio.create_task(self.serve(rid, port)))

    async def serve(self, rid, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.ports[port])

        async def up():
            while (data := await self.ptcp.recv_realm(rid)) is not None:
                writer.write(data)
                await writer.drain()

        t = asyncio.create_task(up())
        while data := await reader.read(65536):
            self.ptcp.send_realm(rid, data)
        self.ptcp._queue_send.put_nowait(
            RawBody(struct.pack("!B3xL", 0x12, rid) + b"DISC"))
        t.cancel()
        writer.close()


async def scenario():
    http = await asyncio.start_server(http_handler, "127.0.0.1", 0)
    echo = await asyncio.start_server(echo_handler, "127.0.0.1", 0)
    ports = {80: http.sockets[0].getsockname()[1],
             554: echo.sockets[0].getsockname()[1]}

    a = await UdpSocket.create(Address.create_from_ip_and_port("127.0.0.1", 1))
    b = await UdpSocket.create(Address.create_from_ip_and_port("127.0.0.1", 1))
    a.set_address_remote(Address.create_from_ip_and_port(
        "127.0.0.1", b.get_address_local().get_port()))
    b.set_address_remote(Address.create_from_ip_and_port(
        "127.0.0.1", a.get_address_local().get_port()))

    dev_ptcp = MuxPtcp(b)
    FakeDevice(dev_ptcp, ports)
    await dev_ptcp.connect()

    s = P2PSession("TESTSERIAL", USER, PASSWORD)
    s.ptcp = MuxPtcp(a)
    await s.ptcp.connect()

    # 1) Tünel üzerinden Digest HTTP
    status, text = await s.http_get(
        "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
    assert status == 200 and "Otopark" in text

    # 2) Yanlış şifre: 401 döner, tekrar denenmez
    s.password = "yanlis"
    status, _ = await s.http_get("/cgi-bin/magicBox.cgi?action=getDeviceType")
    assert status == 401

    # 3) Port yönlendirme: iki eşzamanlı bağlantı, büyük veri, sıra korunur
    lport = await s.forward(554)

    async def roundtrip(size):
        payload = os.urandom(size)
        r, w = await asyncio.open_connection("127.0.0.1", lport)
        w.write(payload)
        await w.drain()
        got = await asyncio.wait_for(r.readexactly(size), 30)
        w.close()
        return got == payload

    assert all(await asyncio.gather(roundtrip(300_000), roundtrip(120_000)))

    await s.close()
    await dev_ptcp.disconnect()
    http.close()
    echo.close()


def test_p2p_mux():
    asyncio.run(asyncio.wait_for(scenario(), 60))
