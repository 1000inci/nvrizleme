"""Dahua P2P (Easy4IP) bağlantı katmanı.

Seri numarasıyla NVR'a bağlanır ve NVR'ın portlarını (80 HTTP, 554 RTSP)
bilgisayarda 127.0.0.1 üzerinde yerel bir porta taşır. Böylece programın
geri kalanı (VLC, RTSP testi) P2P'yi hiç bilmeden normal RTSP ile çalışır.

Sinyalleşme ve PTCP paket biçimi için açık kaynaklı OpenDahua (MIT)
kütüphanesi kullanılır. Bu dosya onun üstüne şunları ekler:
  * aynı tünelden birden çok bağlantı (her biri ayrı "realm"),
  * büyük verinin parçalanarak gönderilmesi,
  * tünel üzerinden Digest kimlik doğrulamalı HTTP GET,
  * yerel TCP portu -> NVR portu yönlendirmesi.

Resmi bir Dahua arayüzü değildir; protokol belgesizdir.
"""
from __future__ import annotations

import asyncio
import random
import socket
import struct
from typing import Callable

from . import _compat  # noqa: F401  (opendahua'yı Python < 3.14'te çalıştırır)
from opendahua.common_object.dahua_error import DahuaError
from opendahua.dahua.dahua_device import DahuaDevice
from opendahua.dahua.dahua_peer_to_peer_connection_error import (
    DahuaPeerToPeerConnectionError,
)
from opendahua.ptcp import ptcp_packet_body_parser as _body_parser_mod
from opendahua.ptcp.ptcp_packet_body import PtcpPacketBody
from opendahua.ptcp.ptcp_packet_body_bind import PtcpPacketBodyBind
from opendahua.ptcp.ptcp_packet_body_data import PtcpPacketBodyData
from opendahua.ptcp.ptcp_packet_body_parser import PtcpPacketBodyParser
from opendahua.ptcp.ptcp_packet_parser import PtcpPacketParser
from opendahua.ptcp.ptcp_realm_identifier import PtcpRealmIdentifier
from opendahua.ptcp.ptcp_socket import PtcpSocket
from opendahua.signaling_client import SignalingClient

from .rtsp import digest_header

Log = Callable[[str], None]

CHUNK = 1024            # tek PTCP paketine konan en fazla veri (sınır 1280)
HTTP_TIMEOUT = 15


class P2PError(Exception):
    pass


class RawBody(PtcpPacketBody):
    """OpenDahua'nın tanımadığı paket gövdesi (ör. durum / bind)."""


# OpenDahua bilinmeyen paket tipinde istisna fırlatıp alım döngüsünü
# öldürüyor. Bunun yerine gövdeyi ham olarak taşıyoruz ki akış sayacı
# (offset) bozulmasın.
_original_parse = PtcpPacketBodyParser.parse.__func__


def _tolerant_parse(cls, body_bytes: bytes) -> PtcpPacketBody:
    try:
        body = _original_parse(cls, body_bytes)
    except Exception:
        return RawBody(body_bytes)
    if type(body).__name__ == "PtcpPacketBodyPortBindConnectionStatus":
        return RawBody(body_bytes)
    return body


PtcpPacketBodyParser.parse = classmethod(_tolerant_parse)
_body_parser_mod.PtcpPacketBodyParser = PtcpPacketBodyParser


def _realm_of(raw: bytes) -> int | None:
    if len(raw) >= 8:
        return struct.unpack("!L", raw[4:8])[0]
    return None


class MuxPtcp(PtcpSocket):
    """Tek PTCP tüneli üzerinde birden çok bağımsız bağlantı."""

    def __init__(self, udp_socket, log: Log = lambda s: None) -> None:
        super().__init__(udp_socket)
        self._realms: dict[int, asyncio.Queue] = {}
        self._log = log
        self.on_control: Callable[[bytes], None] | None = None
        self.last_rx = 0.0

    # --- realm yönetimi -------------------------------------------------
    def open_realm(self, port: int) -> int:
        rid = random.randint(1, 0xFFFFFFFF)
        self._realms[rid] = asyncio.Queue()
        self._queue_send.put_nowait(
            PtcpPacketBodyBind(PtcpRealmIdentifier(rid), port))
        return rid

    def adopt_realm(self, rid: int) -> None:
        self._realms.setdefault(rid, asyncio.Queue())

    def send_realm(self, rid: int, data: bytes) -> None:
        realm = PtcpRealmIdentifier(rid)
        for i in range(0, len(data), CHUNK):
            self._queue_send.put_nowait(
                PtcpPacketBodyData(realm, data[i:i + CHUNK]))

    async def recv_realm(self, rid: int, timeout: float | None = None) -> bytes | None:
        """Veri döner; karşı taraf bağlantıyı kapattıysa None."""
        q = self._realms.get(rid)
        if q is None:
            return None
        return await asyncio.wait_for(q.get(), timeout)

    def close_realm(self, rid: int) -> None:
        self._realms.pop(rid, None)

    @property
    def pending(self) -> int:
        return self._queue_send.qsize() + len(self._all_packet_unacked)

    # --- PtcpSocket davranışını sağlamlaştırma --------------------------
    def _handle_receive(self, data: bytes) -> None:
        try:
            packet = PtcpPacketParser.parse(data)
        except Exception as e:  # bozuk / PTCP olmayan paket
            self._log(f"[PTCP] çözülemeyen paket atlandı: {e}")
            return
        self.last_rx = asyncio.get_event_loop().time()
        self._handle_packet(packet)

    def _handle_packet(self, packet) -> None:
        self._update_packet_identifier_local_received_last_if_needed(packet)
        self._handle_ack(packet.get_offset_received())
        offset = packet.get_offset_sent()
        if offset >= self._offset_received:
            self._buffer_receive[offset] = packet
        elif not packet.get_body().is_empty():
            # Yinelenen paket: onayımız kaybolmuş olabilir, yeniden onayla.
            self._send_ack()
        self._drain_receive_buffer()

    def _drain_receive_buffer(self) -> None:
        got_data = False
        while self._offset_received in self._buffer_receive:
            packet = self._buffer_receive.pop(self._offset_received)
            body = packet.get_body()
            self._offset_received += len(body)
            if body.is_empty():
                continue
            got_data = True
            raw = body.get_ptcp_packet_body_bytes()
            if isinstance(body, PtcpPacketBodyData):
                q = self._realms.get(_realm_of(raw))
                if q is not None:
                    q.put_nowait(raw[12:])
            elif isinstance(body, RawBody):
                self._handle_control(raw)
        if got_data:
            self._send_ack()

    def _handle_control(self, raw: bytes) -> None:
        if self.on_control is not None:
            self.on_control(raw)
            return
        if raw[:1] == b"\x12":
            rid = _realm_of(raw)
            if b"DISC" in raw and rid in self._realms:
                self._realms[rid].put_nowait(None)
            self._log(f"[PTCP] durum paketi: {raw[8:].decode('ascii', 'replace')!r}")


class P2PSession:
    """Bir NVR ile P2P oturumu. Tüm yöntemler aynı asyncio döngüsünde çağrılmalı."""

    def __init__(self, serial: str, user: str, password: str,
                 log: Log = lambda s: None) -> None:
        self.serial = serial.strip().upper()
        self.user = user
        self.password = password
        self.log = log
        self._device = DahuaDevice(self.serial, user, password)
        self._signaling = SignalingClient(self._device)
        self._salt = None
        self.ptcp: MuxPtcp | None = None
        self._servers: list[asyncio.AbstractServer] = []

    # --- bağlantı adımları ----------------------------------------------
    async def check_online(self) -> None:
        """Şifre gerektirmez: Easy4IP sunucusuna ulaşır ve cihazı sorar."""
        try:
            # Windows'ta UDP gönderimi alan adı kabul etmez; önce IP'ye çevir.
            host = SignalingClient.MAIN_REMOTE_HOST
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, SignalingClient.MAIN_REMOTE_PORT,
                family=socket.AF_INET, type=socket.SOCK_DGRAM)
            self._signaling.MAIN_REMOTE_HOST = infos[0][4][0]
            self.log(f"Easy4IP sunucusuna bağlanılıyor: {host} "
                     f"({infos[0][4][0]}, UDP 8800)...")
            main = await asyncio.wait_for(self._signaling._probe(), 15)
        except (asyncio.TimeoutError, TimeoutError):
            raise P2PError(
                "Easy4IP sunucusu yanıt vermedi. İnternet bağlantısını ve "
                "güvenlik duvarının dışarı yönlü UDP 8800'e izin verdiğini "
                "kontrol edin.")
        except OSError as e:
            raise P2PError(f"Easy4IP sunucusuna ulaşılamadı: {e}")
        try:
            self.log(f"Cihaz sorgulanıyor: {self.serial}")
            self._salt = await asyncio.wait_for(
                self._signaling._determine_random_salt(main), 15)
        except (asyncio.TimeoutError, TimeoutError):
            raise P2PError("Cihaz sorgusu zaman aşımına uğradı.")
        except DahuaError as e:
            raise P2PError(
                "Cihaz P2P sunucusunda bulunamadı veya çevrimdışı. Seri "
                "numarasını ve NVR'da Ağ > P2P durumunun 'Çevrimiçi' "
                f"olduğunu kontrol edin. (Sunucu yanıtı: {e})")
        finally:
            main.close()

    async def open_tunnel(self) -> None:
        """Şifreyle kimlik doğrular, UDP delik açma ile tüneli kurar."""
        if self._salt is None:
            await self.check_online()
        self.log("P2P tüneli kuruluyor (UDP delik açma)...")
        try:
            base = await self._signaling.determine_ptcp_socket_device(self._salt)
        except DahuaPeerToPeerConnectionError:
            raise P2PError(
                "Cihazla doğrudan UDP bağlantısı kurulamadı. Bu genelde "
                "taraflardan birinin 'simetrik NAT' (ör. mobil hat / CGNAT) "
                "arkasında olması demektir; bu durumda aktarma (relay) modu "
                "gerekir ve henüz desteklenmiyor.")
        except DahuaError as e:
            raise P2PError(
                "P2P kanalı açılamadı. Kullanıcı adı / şifre hatalı olabilir. "
                f"(Sunucu yanıtı: {e})")
        except (asyncio.TimeoutError, TimeoutError):
            raise P2PError("P2P kanal isteği zaman aşımına uğradı.")
        self.ptcp = MuxPtcp(base._udp_socket, self.log)
        await self.ptcp.connect()
        self.log("P2P tüneli kuruldu.")

    @property
    def alive(self) -> bool:
        if self.ptcp is None:
            return False
        now = asyncio.get_event_loop().time()
        return self.ptcp.last_rx == 0.0 or now - self.ptcp.last_rx < 30

    # --- tünel üzerinden HTTP -------------------------------------------
    async def _http_once(self, path: str, auth: str | None) -> tuple[int, dict, bytes]:
        assert self.ptcp is not None
        rid = self.ptcp.open_realm(80)
        try:
            req = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   f"User-Agent: NVRIzleme\r\nConnection: close\r\n")
            if auth:
                req += f"Authorization: {auth}\r\n"
            self.ptcp.send_realm(rid, (req + "\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = await self.ptcp.recv_realm(rid, HTTP_TIMEOUT)
                if chunk is None:
                    break
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            lines = head.decode("utf-8", "replace").split("\r\n")
            if not lines[0].startswith("HTTP/"):
                raise P2PError("Tünelden geçersiz HTTP yanıtı geldi.")
            status = int(lines[0].split()[1])
            headers: dict[str, str] = {}
            for line in lines[1:]:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0") or 0)
            while len(body) < length:
                chunk = await self.ptcp.recv_realm(rid, HTTP_TIMEOUT)
                if chunk is None:
                    break
                body += chunk
            return status, headers, body
        except (asyncio.TimeoutError, TimeoutError):
            raise P2PError("Tünel üzerinden HTTP yanıtı gelmedi (zaman aşımı).")
        finally:
            self.ptcp.close_realm(rid)

    async def http_get(self, path: str) -> tuple[int, str]:
        """Digest kimlik doğrulamalı GET. 401'de yeniden denemez."""
        status, headers, body = await self._http_once(path, None)
        if status == 401:
            challenge = headers.get("www-authenticate", "")
            if not challenge.lower().startswith("digest"):
                raise P2PError("Cihaz Digest kimlik doğrulama sunmadı.")
            auth = digest_header(self.user, self.password, "GET", path, challenge)
            status, headers, body = await self._http_once(path, auth)
        return status, body.decode("utf-8", "replace")

    # --- yerel port yönlendirme -----------------------------------------
    async def forward(self, remote_port: int = 554, local_port: int = 0) -> int:
        """127.0.0.1:<dönen port> -> NVR:<remote_port>. Yerel portu döndürür."""
        assert self.ptcp is not None
        ptcp = self.ptcp

        async def handle(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
            rid = ptcp.open_realm(remote_port)

            async def up() -> None:
                while True:
                    data = await reader.read(16384)
                    if not data:
                        return
                    ptcp.send_realm(rid, data)
                    while ptcp.pending > 256:      # geri basınç
                        await asyncio.sleep(0.01)

            async def down() -> None:
                while True:
                    data = await ptcp.recv_realm(rid)
                    if data is None:
                        return
                    writer.write(data)
                    await writer.drain()

            tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                ptcp.close_realm(rid)
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", local_port)
        self._servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        for s in self._servers:
            s.close()
        self._servers.clear()
        if self.ptcp is not None:
            await self.ptcp.disconnect()
            self.ptcp = None
