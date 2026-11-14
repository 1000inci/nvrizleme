"""Cihaz bağlantıları: arka plan iş parçacığında P2P tünelleri / IP bağlantıları.

Arayüz (Qt) ana iş parçacığında, tüneller ayrı bir asyncio döngüsünde çalışır.
Sonuçlar Bridge sinyalleriyle arayüze iletilir.
"""
from __future__ import annotations

import asyncio
import sys
import threading

from PySide6.QtCore import QObject, Signal

from . import rtsp
from .dahua import AuthError, DahuaClient, DahuaError, parse_channel_titles, parse_kv
from .p2p import P2PAuthError, P2PError, P2PSession
from .store import Device

CHANNEL_TITLES = "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"


class Bridge(QObject):
    log = Signal(str)
    status = Signal(str, str)                 # cihaz id, durum metni
    connected = Signal(str, str, int, list)   # cihaz id, RTSP adresi, port, kanal adları
    lost = Signal(str)                        # tünel koptu; yeniden bağlanılıyor
    failed = Signal(str, str)                 # cihaz id, hata (otomatik deneme yok)


class LinkManager:
    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self._futures: dict[str, object] = {}
        self._loop = (asyncio.SelectorEventLoop() if sys.platform == "win32"
                      else asyncio.new_event_loop())
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        name="nvr-links", daemon=True)
        self._thread.start()

    # --- arayüzden çağrılanlar ------------------------------------------
    def connect(self, dev: Device, password: str) -> None:
        self.disconnect(dev.id)
        coro = self._run_p2p(dev, password) if dev.kind == "p2p" \
            else self._run_ip(dev, password)
        self._futures[dev.id] = asyncio.run_coroutine_threadsafe(coro, self._loop)

    def disconnect(self, device_id: str) -> None:
        fut = self._futures.pop(device_id, None)
        if fut is not None:
            fut.cancel()

    def shutdown(self) -> None:
        for device_id in list(self._futures):
            self.disconnect(device_id)

        async def settle() -> None:
            await asyncio.sleep(0.3)        # iptal edilen görevler tüneli kapatsın

        try:
            asyncio.run_coroutine_threadsafe(settle(), self._loop).result(2)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(2)

    # --- P2P ------------------------------------------------------------
    async def _run_p2p(self, dev: Device, password: str) -> None:
        b = self.bridge
        tag = f"[{dev.name}] "
        delay = 5
        ever_connected = False
        while True:
            s = P2PSession(dev.serial, dev.user, password,
                           lambda m: b.log.emit(tag + m))
            try:
                b.status.emit(dev.id, "bağlanıyor…")
                await s.check_online()
                await s.open_tunnel()
                port = await s.forward()
                names: list[str] = []
                try:
                    status, text = await s.http_get(CHANNEL_TITLES)
                    if status == 200:
                        names = [c.name for c in parse_channel_titles(parse_kv(text))]
                    else:
                        b.log.emit(tag + f"Kanal adları okunamadı (HTTP {status}); "
                                         "kanal sayısı ayardan alınacak.")
                except P2PError as e:
                    b.log.emit(tag + f"Kanal adları okunamadı: {e}")
                ever_connected = True
                delay = 5
                b.connected.emit(dev.id, "127.0.0.1", port, names)
                while s.alive:
                    await asyncio.sleep(2)
                b.log.emit(tag + "Tünel koptu (30 sn'dir paket gelmiyor).")
                b.lost.emit(dev.id)
            except P2PAuthError as e:
                b.failed.emit(dev.id, str(e))
                return
            except P2PError as e:
                if not ever_connected:
                    b.failed.emit(dev.id, str(e))
                    return
                b.log.emit(tag + f"Yeniden bağlanma başarısız: {e}")
                b.lost.emit(dev.id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # beklenmeyen hata arayüzü sessiz bırakmasın
                b.failed.emit(dev.id, f"Beklenmeyen hata: {e!r}")
                return
            finally:
                try:
                    await s.close()
                except Exception:
                    pass
            b.status.emit(dev.id, f"{delay} sn sonra yeniden bağlanacak…")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    # --- doğrudan IP ----------------------------------------------------
    async def _run_ip(self, dev: Device, password: str) -> None:
        b = self.bridge
        tag = f"[{dev.name}] "
        loop = asyncio.get_running_loop()
        b.status.emit(dev.id, "bağlanıyor…")
        try:
            res = await loop.run_in_executor(
                None, lambda: rtsp.probe(dev.host, dev.user, password, 1,
                                         rtsp.SUB, dev.rtsp_port))
            if not res.ok:
                b.failed.emit(dev.id, res.message)
                return
            b.log.emit(tag + res.message)

            def titles() -> list[str]:
                client = DahuaClient(dev.host, dev.http_port, dev.user, password)
                return [c.name for c in client.channels()]

            names: list[str] = []
            try:
                names = await loop.run_in_executor(None, titles)
            except AuthError as e:
                b.log.emit(tag + f"HTTP girişi reddedildi: {e}")
            except DahuaError as e:
                b.log.emit(tag + f"Kanal adları okunamadı ({e}); "
                                 "kanal sayısı ayardan alınacak.")
            b.connected.emit(dev.id, dev.host, dev.rtsp_port, names)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            b.failed.emit(dev.id, f"Beklenmeyen hata: {e!r}")
