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
import os
import random
import socket
import struct
from typing import Callable

from . import _compat  # noqa: F401  (opendahua'yı Python < 3.14'te çalıştırır)
from opendahua.api_peer_to_peer.api_peer_to_peer_authentication_util import (
    ApiPeerToPeerAuthenticationUtil,
)
from opendahua.api_peer_to_peer.request.api_peer_to_peer_encryption_util import (
    ApiPeerToPeerEncryptionUtil,
)
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
from opendahua.udp.udp_protocol import UdpProtocol

from .rtsp import digest_header

Log = Callable[[str], None]

CHUNK = 1024            # tek PTCP paketine konan en fazla veri (sınır 1280)
HTTP_TIMEOUT = 15
RETRY_GAP = 8          # tünel denemeleri arası bekleme (sn); cihazı yormamak için


class P2PError(Exception):
    pass


class P2PAuthError(P2PError):
    """Kanal isteği reddedildi (büyük olasılıkla şifre). Otomatik yeniden denenmemeli."""


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


# Windows, UDP'de "port ulaşılamaz" (ICMP) yanıtını bir sonraki okumada
# WinError 10054 olarak bildirir. Delik açma sırasında ilk paketlerin
# reddedilmesi olağandır; bu hatayı yok sayıp beklemeye devam ediyoruz.
_original_error_received = UdpProtocol.error_received


def _tolerant_error_received(self, exc: Exception) -> None:
    if isinstance(exc, ConnectionResetError):
        return
    _original_error_received(self, exc)


UdpProtocol.error_received = _tolerant_error_received


# Cihaz bilgisi (HTTP/RTSP portları dahil) OpenDahua içinde çözülüyor ama
# yalnızca "randsalt" saklanıyor. Portlar varsayılan olmayabildiği için
# (ör. HTTP 7980, RTSP 7982) çözülen bilginin tamamını da yakalıyoruz.
_last_device_info: dict = {}
_original_decrypt_info = ApiPeerToPeerEncryptionUtil.decrypt_device_info.__func__


def _capturing_decrypt_info(cls, device_info_encrypted: str) -> dict:
    if not device_info_encrypted:
        # Eski yazılımlar bilgi vermez (<Info/> boş gelir): tuz yoktur.
        info = {"randsalt": ""}
    else:
        info = _original_decrypt_info(cls, device_info_encrypted)
    _last_device_info.clear()
    _last_device_info.update(info)
    return info


ApiPeerToPeerEncryptionUtil.decrypt_device_info = classmethod(_capturing_decrypt_info)


# Tuz bildirmeyen cihazlar kimlik gövdesinde <RandSalt> öğesini boş değil,
# hiç görmek ister.
_original_auth_body = ApiPeerToPeerAuthenticationUtil.generate_part_body_authentication.__func__


def _auth_body_without_empty_salt(cls, *args, **kwargs) -> str:
    body = _original_auth_body(cls, *args, **kwargs)
    return body.replace("<RandSalt></RandSalt>", "")


ApiPeerToPeerAuthenticationUtil.generate_part_body_authentication = classmethod(
    _auth_body_without_empty_salt)


def _realm_of(raw: bytes) -> int | None:
    if len(raw) >= 8:
        return struct.unpack("!L", raw[4:8])[0]
    return None


class MuxPtcp(PtcpSocket):
    """Tek PTCP tüneli üzerinde birden çok bağımsız bağlantı."""

    def __init__(self, udp_socket, log: Log = lambda s: None) -> None:
        super().__init__(udp_socket)
        self._realms: dict[int, asyncio.Queue] = {}
        self._remote_closed: set[int] = set()
        self._log = log
        self.on_control: Callable[[bytes], None] | None = None
        self.last_rx = 0.0

    # --- döngüleri sağlamlaştırma ---------------------------------------
    # OpenDahua'da bu döngülerden biri istisna alırsa sessizce ölür; örneğin
    # gönderim döngüsü ölürse eski akışlar sürer ama yenisi hiç açılamaz.
    # Burada istisna günlüğe yazılır ve döngü çalışmaya devam eder.
    async def _guard(self, name: str, step) -> None:
        while True:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"[PTCP] {name} hatası (devam ediliyor): {e!r}")
                await asyncio.sleep(0.05)

    async def _loop_send(self) -> None:
        async def step() -> None:
            body = await self._queue_send.get()
            self._send_packet_body(body)
        await self._guard("gönderim", step)

    async def _loop_receive(self) -> None:
        async def step() -> None:
            self._handle_receive(await self._udp_socket.receive())
        await self._guard("alım", step)

    async def _loop_retransmit(self) -> None:
        async def step() -> None:
            await asyncio.sleep(self.TIMEOUT_NUMBER_OF_SECOND_ACK)
            self._handle_retransmit()
        await self._guard("yeniden gönderim", step)

    async def _loop_heartbeat(self) -> None:
        async def step() -> None:
            await asyncio.sleep(self.INTERVAL_NUMBER_OF_SECOND_HEARTBEAT)
            self._send_heartbeat()
        await self._guard("kalp atışı", step)

    def _tag(self, rid: int) -> str:
        return f"akış {rid & 0xFFFF:04x}"

    # --- realm yönetimi -------------------------------------------------
    def open_realm(self, port: int) -> int:
        rid = random.randint(1, 0xFFFFFFFF)
        self._realms[rid] = asyncio.Queue()
        self._log(f"[PTCP] {self._tag(rid)} açılıyor (port {port}; açık: "
                  f"{len(self._realms)}, bekleyen paket: {self.pending})")
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
        """Bağlantıyı kapatır ve cihaza bildirir (cihazda açık oturum kalmasın)."""
        if self._realms.pop(rid, None) is None:
            return
        if rid in self._remote_closed:
            self._remote_closed.discard(rid)
            return
        if os.environ.get("NVRIZLEME_NO_DISC"):
            self._log(f"[PTCP] {self._tag(rid)} kapatıldı (cihaza bildirilmedi)")
            return
        self._log(f"[PTCP] {self._tag(rid)} kapatıldı, cihaza bildirildi")
        self._queue_send.put_nowait(
            RawBody(struct.pack("!B3xLL", 0x12, rid, 0) + b"DISC"))

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
            known = rid in self._realms
            if b"DISC" in raw and known:
                self._remote_closed.add(rid)
                self._realms[rid].put_nowait(None)
            word = "CONN" if b"CONN" in raw else "DISC" if b"DISC" in raw else "?"
            self._log(f"[PTCP] {self._tag(rid or 0)} cihaz yanıtı: {word}"
                      f"{'' if known else ' (kapanmış akış)'}")


class _repeat_first_packet:
    """İlk el sıkışma paketini yanıt gelene kadar aralıklarla yeniden yollar.

    OpenDahua ilk paketi tek kez gönderip 1 sn bekler. UDP'de tek paket
    kolayca kaybolur ya da karşı tarafın NAT kaydı henüz açılmamış olur;
    bu yüzden bağlantı çoğu denemede kurulamıyordu.
    """
    INTERVAL = 0.5
    COUNT = 5

    def __init__(self, udp) -> None:
        self._udp = udp
        self._task: asyncio.Task | None = None
        self._original_send = udp.send
        udp.send = self._send

    def _send(self, data: bytes) -> None:
        self._original_send(data)
        if self._task is None:              # ilk paket
            self._task = asyncio.create_task(self._repeat(data))
        else:                               # el sıkışma ilerledi
            self._task.cancel()

    async def _repeat(self, data: bytes) -> None:
        for _ in range(self.COUNT):
            await asyncio.sleep(self.INTERVAL)
            if self._udp._protocol.queue_receive.qsize():
                return                      # yanıt geldi
            self._original_send(data)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self._udp.send = self._original_send


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
        self.http_port = 80      # check_online cihazın bildirdiği değerle günceller
        self.rtsp_port = 554
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
        try:
            self.http_port = int(_last_device_info.get("httpport") or 80)
            self.rtsp_port = int(_last_device_info.get("rtspport") or 554)
        except (TypeError, ValueError):
            pass
        self.log(f"Cihazın bildirdiği portlar: HTTP {self.http_port}, "
                 f"RTSP {self.rtsp_port}")

    async def open_tunnel(self, attempts: int = 3) -> None:
        """Şifreyle kimlik doğrular, UDP delik açma ile tüneli kurar."""
        if self._salt is None:
            await self.check_online()
        sig = self._signaling
        base = None
        for attempt in range(1, attempts + 1):
            self.log(f"Kanal isteği gönderiliyor (deneme {attempt}/{attempts})...")
            try:
                udp, local = await sig._determine_udp_socket_device(self._salt)
            except DahuaError as e:
                raise P2PAuthError(
                    "P2P kanalı açılamadı. Kullanıcı adı / şifre hatalı "
                    f"olabilir; tekrar denemeden önce kontrol edin. ({e})")
            except (asyncio.TimeoutError, TimeoutError):
                self.log("Sunucu kanal isteğine yanıt vermedi.")
                if attempt < attempts:
                    await asyncio.sleep(RETRY_GAP)
                continue
            self.log("Kanal açıldı (şifre kabul edildi). Cihaz adresi: "
                     f"dış {udp.get_address_remote().get_address_string()}, "
                     f"iç {local.get_address_string()}")
            self.log("Cihaza doğrudan UDP bağlantısı deneniyor...")
            repeater = _repeat_first_packet(udp)
            sig.TIME_NUMBER_OF_SECOND_TIMEOUT_HANDSHAKE = 4
            try:
                base = await sig._determine_ptcp_socket_device(udp, local)
                repeater.stop()
                break
            except DahuaPeerToPeerConnectionError:
                repeater.stop()
                self.log("Cihazdan hiç yanıt gelmedi.")
                udp.close()
            except (asyncio.TimeoutError, TimeoutError):
                repeater.stop()
                self.log("Cihazdan ilk yanıt GELDİ ama el sıkışma yarıda kaldı.")
                udp.close()
            if attempt < attempts:
                await asyncio.sleep(RETRY_GAP)
        if base is None:
            raise P2PError(
                f"{attempts} denemede P2P tüneli kurulamadı. Günlükteki "
                "satırlar hangi aşamada kalındığını gösterir. Hep 'cihazdan "
                "hiç yanıt gelmedi' ise arada doğrudan bağlantıyı engelleyen "
                "bir güvenlik duvarı / NAT vardır.")
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
        rid = self.ptcp.open_realm(self.http_port)
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
    async def forward(self, remote_port: int | None = None, local_port: int = 0) -> int:
        """127.0.0.1:<dönen port> -> NVR:<remote_port>. Yerel portu döndürür.

        remote_port verilmezse cihazın RTSP portu kullanılır.
        """
        assert self.ptcp is not None
        if remote_port is None:
            remote_port = self.rtsp_port
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
