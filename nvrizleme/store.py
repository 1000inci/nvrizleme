"""Cihaz listesi (JSON) ve şifre saklama (Windows Kimlik Yöneticisi)."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

SERVICE = "NVRIzleme"


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "NVRIzleme"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Device:
    name: str
    kind: str = "p2p"            # "p2p" (seri no) veya "ip" (adres)
    serial: str = ""
    host: str = ""
    http_port: int = 80
    rtsp_port: int = 554
    user: str = "admin"
    channel_count: int = 16      # cihaz kanal listesi vermezse kullanılır
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def target(self) -> str:
        return self.serial if self.kind == "p2p" else self.host


def load_devices() -> list[Device]:
    path = config_dir() / "devices.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    known = set(Device.__dataclass_fields__)
    out = []
    for item in raw:
        try:
            out.append(Device(**{k: v for k, v in item.items() if k in known}))
        except TypeError:
            continue
    return out


def save_devices(devices: list[Device]) -> None:
    path = config_dir() / "devices.json"
    path.write_text(json.dumps([asdict(d) for d in devices], indent=2,
                               ensure_ascii=False), encoding="utf-8")


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def keyring_available() -> bool:
    return _keyring() is not None


def get_password(device_id: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, device_id)
    except Exception:
        return None


def set_password(device_id: str, password: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE, device_id, password)
        return True
    except Exception:
        return False


def delete_password(device_id: str) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE, device_id)
    except Exception:
        pass
