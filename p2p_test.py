"""P2P bağlantı testi - NVR'a seri numarasıyla adım adım bağlanır.

Kullanım (Windows komut satırı):
    python p2p_test.py 8B07084PAZEAA6C
    python p2p_test.py 8B07084PAZEAA6C --kullanici admin --izle 1
    python p2p_test.py 8B07084PAZEAA6C --sadece-durum      (şifre sormaz)
    python p2p_test.py 8B07084PAZEAA6C --ayrinti           (paket dökümü)

Şifre komut satırına yazılmaz; program ekranda gizli olarak sorar.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import shutil
import subprocess
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser(description="Dahua NVR P2P bağlantı testi")
    ap.add_argument("seri", help="NVR seri numarası")
    ap.add_argument("--kullanici", default="admin")
    ap.add_argument("--sadece-durum", action="store_true",
                    help="yalnızca cihaz P2P'de çevrimiçi mi diye bakar")
    ap.add_argument("--izle", type=int, metavar="KANAL", default=0,
                    help="tüneli açık tutar ve bu kanalı VLC'de açar")
    ap.add_argument("--ana", action="store_true",
                    help="--izle için ana akış (varsayılan: alt akış)")
    ap.add_argument("--ayrinti", action="store_true")
    args = ap.parse_args()

    if args.ayrinti:
        os.environ["OPENDAHUA_LOG_LEVEL"] = "DEBUG"

    # Ortam değişkeni ayarlandıktan sonra içe aktarılmalı.
    from nvrizleme import rtsp
    from nvrizleme.dahua import parse_channel_titles, parse_kv
    from nvrizleme.p2p import P2PError, P2PSession

    t0 = time.monotonic()

    def log(msg: str) -> None:
        print(f"  [{time.monotonic() - t0:6.2f}s] {msg}")

    def step(n: int, title: str) -> None:
        print(f"\nADIM {n}: {title}")

    async def run() -> int:
        s = P2PSession(args.seri, args.kullanici, "", log)
        try:
            step(1, "Cihaz P2P sunucusunda kayıtlı ve çevrimiçi mi? (şifresiz)")
            await s.check_online()
            print("  TAMAM - cihaz P2P sunucusunda bulundu.")
            if args.sadece_durum:
                return 0

            print("\nDikkat: art arda hatalı şifre NVR hesabını kilitleyebilir.")
            password = getpass.getpass(f"'{args.kullanici}' şifresi: ")
            s.password = password
            s._device._password = password

            step(2, "P2P tüneli (kimlik doğrulama + UDP delik açma)")
            await s.open_tunnel()
            print("  TAMAM - tünel açık.")

            step(3, "Tünel üzerinden cihaz bilgisi (HTTP)")
            status, text = await s.http_get(
                "/cgi-bin/magicBox.cgi?action=getDeviceType")
            if status == 401:
                print("  HATA - HTTP şifresi reddedildi (401). Tekrar denemeyin; "
                      "şifreyi kontrol edin.")
                return 2
            if status != 200:
                print(f"  UYARI - HTTP {status}")
            else:
                print(f"  Model: {parse_kv(text).get('type', '?')}")
                status, text = await s.http_get(
                    "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
                chans = parse_channel_titles(parse_kv(text)) if status == 200 else []
                print(f"  Kanal sayısı: {len(chans)}")
                for ch in chans:
                    print(f"    {ch.number:>2}: {ch.name}")

            step(4, "RTSP akışı (yerel port üzerinden)")
            lport = await s.forward(554)
            log(f"Yerel RTSP portu: 127.0.0.1:{lport}")
            channel = args.izle or 1
            subtype = rtsp.MAIN if args.ana else rtsp.SUB
            res = await asyncio.get_running_loop().run_in_executor(
                None, lambda: rtsp.probe("127.0.0.1", args.kullanici, password,
                                         channel, subtype, lport, 15))
            print(f"  {'TAMAM' if res.ok else 'HATA'} - {res.message}")
            if not res.ok:
                return 3

            print("\nSONUÇ: P2P bağlantısı çalışıyor.")
            if not args.izle:
                print("Görüntüyü izlemek için:  --izle 1  ekleyin.")
                return 0

            url = rtsp.live_url("127.0.0.1", channel, subtype, lport)
            print(f"\nVLC adresi: {url}")
            print("VLC kullanıcı adı / şifre sorarsa NVR bilgilerini girin.")
            vlc = shutil.which("vlc") or next(
                (p for p in (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                             r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe")
                 if os.path.exists(p)), None)
            if vlc:
                subprocess.Popen([vlc, "--rtsp-tcp", url])
            else:
                print("VLC bulunamadı; adresi VLC'de 'Ağ Akışı Aç' ile açın "
                      "(Araçlar > Tercihler > Girdi: RTP over RTSP (TCP)).")
            print("Tünel açık. Kapatmak için Ctrl+C.")
            while s.alive:
                await asyncio.sleep(2)
            print("Tünel koptu (30 sn'dir cihazdan paket gelmiyor).")
            return 4
        except P2PError as e:
            print(f"  HATA - {e}")
            return 1
        finally:
            await s.close()

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("\nKapatıldı.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
