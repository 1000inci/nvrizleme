"""Doğrudan IP / alan adı ile bağlantı testi (P2P kullanmadan).

NVR'a yerel ağdan, VPN'den ya da port yönlendirmeyle ulaşılabiliyorsa kullanılır.

    python ip_test.py 192.168.1.150
    python ip_test.py ORNEK.ADRES --http-port 7980 --rtsp-port 7982 --izle 1

Şifre komut satırına yazılmaz; program gizli olarak sorar.
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys

from nvrizleme import rtsp
from nvrizleme.dahua import AuthError, DahuaClient, DahuaError


def _print_info(info) -> None:
    print(f"  Model: {info.model}   Seri no: {info.serial}")
    print(f"  Kanal sayısı: {len(info.channels)}")
    known = [c for c in info.channels if c.online is not None]
    if known:
        print(f"  Bağlı kamera: {sum(1 for c in known if c.online)}")
    for c in info.channels:
        durum = {True: "bağlı", False: "bağlı değil", None: ""}[c.online]
        print(f"    {c.number:>3}: {c.name}  {durum}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Dahua NVR doğrudan bağlantı testi")
    ap.add_argument("adres", help="NVR IP adresi veya alan adı")
    ap.add_argument("--kullanici", default="admin")
    ap.add_argument("--http-port", type=int, default=80)
    ap.add_argument("--rtsp-port", type=int, default=554)
    ap.add_argument("--https", action="store_true")
    ap.add_argument("--izle", type=int, metavar="KANAL", default=0)
    ap.add_argument("--ana", action="store_true", help="ana akış (varsayılan: alt)")
    a = ap.parse_args()

    print("Dikkat: art arda hatalı şifre NVR hesabını kilitleyebilir.")
    password = getpass.getpass(f"'{a.kullanici}' şifresi: ")

    print(f"\nADIM 1: Cihaz bilgisi (HTTP {a.http_port})")
    try:
        info = DahuaClient(a.adres, a.http_port, a.kullanici, password,
                           https=a.https).fetch_info()
    except AuthError as e:
        print(f"  HATA - {e}")
        return 2
    except DahuaError as e:
        print(f"  UYARI - {e}")
        print("  Cihaz bilgisi okunamadı. NVR'da CGI servisi kapalı olabilir "
              "(Sistem > Güvenlik > Sistem Servisi > CGI).\n"
              "  RTSP testi yine de denenecek.")
        info = None
    if info is not None:
        _print_info(info)

    channel = a.izle or 1
    subtype = rtsp.MAIN if a.ana else rtsp.SUB
    print(f"\nADIM 2: RTSP akışı (port {a.rtsp_port}, kanal {channel})")
    res = rtsp.probe(a.adres, a.kullanici, password, channel, subtype, a.rtsp_port)
    print(f"  {'TAMAM' if res.ok else 'HATA'} - {res.message}")
    if not res.ok:
        return 3

    print("\nSONUÇ: doğrudan bağlantı çalışıyor.")
    if a.izle:
        url = rtsp.live_url(a.adres, channel, subtype, a.rtsp_port)
        print(f"VLC adresi: {url}\nVLC kullanıcı adı / şifre soracak.")
        vlc = shutil.which("vlc") or next(
            (p for p in (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                         r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe")
             if os.path.exists(p)), None)
        if vlc:
            subprocess.Popen([vlc, "--rtsp-tcp", url])
        else:
            print("VLC bulunamadı; adresi VLC'de 'Ağ Akışı Aç' ile açın.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
