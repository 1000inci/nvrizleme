"""Dahua NVR HTTP API (CGI) istemcisi.

Yalnızca izleme için gereken, okuma amaçlı çağrılar kullanılır:
cihaz tipi, seri no, yazılım sürümü, kanal adları ve kanal bağlantı durumu.

Önemli: Dahua cihazları art arda hatalı şifre denemesinde hesabı
(varsayılan 5 deneme / 30 dakika) kilitler. Bu yüzden bu istemci
401 yanıtında ASLA tekrar denemez; hatayı hemen yukarı bildirir.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests
from requests.auth import HTTPDigestAuth

TIMEOUT = 6  # saniye


class DahuaError(Exception):
    """Genel bağlantı / yanıt hatası."""


class AuthError(DahuaError):
    """Kullanıcı adı veya şifre hatalı (ya da hesap kilitli)."""


@dataclass
class Channel:
    index: int          # 0 tabanlı (CGI); RTSP'de index + 1 kullanılır
    name: str
    state: str = ""     # "Connected", "Unconnected" veya "" (bilinmiyor)

    @property
    def number(self) -> int:
        return self.index + 1

    @property
    def online(self) -> bool | None:
        if not self.state:
            return None
        return self.state.lower() == "connected"


@dataclass
class DeviceInfo:
    model: str = ""
    serial: str = ""
    version: str = ""
    channels: list[Channel] = field(default_factory=list)


def parse_kv(text: str) -> dict[str, str]:
    """Dahua'nın 'anahtar=değer' satır biçimini sözlüğe çevirir."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def parse_channel_titles(kv: dict[str, str]) -> list[Channel]:
    """table.ChannelTitle[N].Name satırlarından kanal listesi çıkarır."""
    chans: dict[int, str] = {}
    for key, value in kv.items():
        if not key.startswith("table.ChannelTitle[") or not key.endswith("].Name"):
            continue
        try:
            idx = int(key[len("table.ChannelTitle["):-len("].Name")])
        except ValueError:
            continue
        chans[idx] = value
    return [Channel(i, chans[i] or f"Kanal {i + 1}") for i in sorted(chans)]


def parse_camera_states(kv: dict[str, str]) -> dict[int, str]:
    """states[N].channel / states[N].connectionState satırlarını eşler."""
    by_slot: dict[str, dict[str, str]] = {}
    for key, value in kv.items():
        if not key.startswith("states["):
            continue
        slot, _, attr = key.partition("].")
        by_slot.setdefault(slot, {})[attr] = value
    result: dict[int, str] = {}
    for item in by_slot.values():
        try:
            result[int(item["channel"])] = item.get("connectionState", "")
        except (KeyError, ValueError):
            continue
    return result


class DahuaClient:
    def __init__(self, host: str, port: int = 80, user: str = "admin",
                 password: str = "", https: bool = False,
                 verify_tls: bool = False):
        scheme = "https" if https else "http"
        self.base = f"{scheme}://{host}:{port}"
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(user, password)
        # NVR'lar genelde kendinden imzalı sertifika kullanır.
        self.session.verify = verify_tls
        if not verify_tls:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _get(self, path: str) -> str:
        url = self.base + path
        try:
            r = self.session.get(url, timeout=TIMEOUT)
        except requests.exceptions.SSLError as e:
            raise DahuaError(f"HTTPS hatası: {e}") from e
        except requests.exceptions.ConnectTimeout as e:
            raise DahuaError("Cihaz yanıt vermiyor (zaman aşımı).") from e
        except requests.exceptions.ConnectionError as e:
            raise DahuaError(f"Bağlanılamadı: {e}") from e
        except requests.exceptions.Timeout as e:
            raise DahuaError("Cihaz yanıtı zaman aşımına uğradı.") from e
        if r.status_code == 401:
            raise AuthError(
                "Kullanıcı adı veya şifre hatalı ya da hesap kilitli. "
                "Dikkat: art arda hatalı denemeler hesabı kilitler.")
        if r.status_code == 403:
            raise AuthError("Erişim reddedildi (kullanıcı yetkisi yetersiz "
                            "veya IP engellenmiş).")
        if r.status_code != 200:
            raise DahuaError(f"HTTP {r.status_code}: {path}")
        r.encoding = r.encoding or "utf-8"
        return r.text

    def get_kv(self, path: str) -> dict[str, str]:
        return parse_kv(self._get(path))

    def device_type(self) -> str:
        return self.get_kv("/cgi-bin/magicBox.cgi?action=getDeviceType").get("type", "")

    def serial(self) -> str:
        return self.get_kv("/cgi-bin/magicBox.cgi?action=getSerialNo").get("sn", "")

    def software_version(self) -> str:
        return self.get_kv(
            "/cgi-bin/magicBox.cgi?action=getSoftwareVersion").get("version", "")

    def channels(self) -> list[Channel]:
        kv = self.get_kv("/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
        chans = parse_channel_titles(kv)
        if not chans:
            raise DahuaError("Kanal listesi alınamadı.")
        return chans

    def camera_states(self) -> dict[int, str]:
        """Kanal bağlantı durumları. Desteklemeyen cihazda boş sözlük döner."""
        try:
            kv = self.get_kv("/cgi-bin/LogicDeviceManager.cgi"
                             "?action=getCameraState&uniqueChannels[0]=-1")
        except AuthError:
            raise
        except DahuaError:
            return {}
        return parse_camera_states(kv)

    def fetch_info(self) -> DeviceInfo:
        """Cihaz bilgisi + kanal listesi. İlk çağrı aynı zamanda giriş testidir."""
        info = DeviceInfo()
        info.model = self.device_type()           # 401 ise burada durur
        try:
            info.serial = self.serial()
        except AuthError:
            raise
        except DahuaError:
            pass
        try:
            info.version = self.software_version()
        except AuthError:
            raise
        except DahuaError:
            pass
        info.channels = self.channels()
        states = self.camera_states()
        for ch in info.channels:
            ch.state = states.get(ch.index, "")
        return info
