"""RTSP adresleri ve görüntü almadan RTSP bağlantı/şifre testi.

Dahua canlı görüntü adresi:
    rtsp://IP:554/cam/realmonitor?channel=<1..N>&subtype=<0 ana | 1 alt>

Şifre adrese GÖMÜLMEZ; oynatıcıya ayrı seçenek olarak verilir. Böylece
şifredeki '@', '#', ':' gibi karakterler adresi bozmaz.
"""
from __future__ import annotations

import hashlib
import re
import socket
from dataclasses import dataclass

MAIN = 0
SUB = 1


def live_url(host: str, channel: int, subtype: int = SUB, port: int = 554) -> str:
    return (f"rtsp://{host}:{port}/cam/realmonitor"
            f"?channel={channel}&subtype={subtype}")


@dataclass
class ProbeResult:
    ok: bool
    message: str
    video_codec: str = ""


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _parse_auth_header(value: str) -> dict[str, str]:
    params = dict(re.findall(r'(\w+)="([^"]*)"', value))
    for k, v in re.findall(r'(\w+)=([^",\s]+)', value):
        params.setdefault(k, v)
    return params


def digest_header(user: str, password: str, method: str, uri: str,
                  challenge: str, nc: int = 1, cnonce: str = "0a4f113b") -> str:
    p = _parse_auth_header(challenge)
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    ha1 = _md5(f"{user}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    qop = p.get("qop", "")
    if "auth" in [q.strip() for q in qop.split(",")] if qop else False:
        ncs = f"{nc:08x}"
        resp = _md5(f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}")
        return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                f'uri="{uri}", response="{resp}", qop=auth, nc={ncs}, '
                f'cnonce="{cnonce}"')
    resp = _md5(f"{ha1}:{nonce}:{ha2}")
    return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp}"')


def _read_response(sock: socket.socket) -> tuple[int, dict[str, list[str]], str]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    if not lines or not lines[0].startswith("RTSP/"):
        raise ConnectionError("Geçersiz RTSP yanıtı.")
    status = int(lines[0].split()[1])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers.setdefault(k.strip().lower(), []).append(v.strip())
    length = int(headers.get("content-length", ["0"])[0] or 0)
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return status, headers, body[:length].decode("utf-8", "replace")


def _codec_from_sdp(sdp: str) -> str:
    in_video = False
    for line in sdp.splitlines():
        if line.startswith("m="):
            in_video = line.startswith("m=video")
        elif in_video and line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:\d+\s+([^/]+)", line)
            if m:
                return m.group(1).upper()
    return ""


def probe(host: str, user: str, password: str, channel: int = 1,
          subtype: int = SUB, port: int = 554, timeout: float = 6) -> ProbeResult:
    """RTSP DESCRIBE gönderir; şifreyi ve akışın varlığını doğrular.

    Görüntü çözmez, bu yüzden VLC kurulu olmasa da çalışır.
    """
    url = live_url(host, channel, subtype, port)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        return ProbeResult(False, f"RTSP portuna ({port}) bağlanılamadı: {e}")
    try:
        sock.settimeout(timeout)
        base = (f"DESCRIBE {url} RTSP/1.0\r\nCSeq: {{cseq}}\r\n"
                f"Accept: application/sdp\r\nUser-Agent: NVRIzleme\r\n")
        sock.sendall((base.format(cseq=1) + "\r\n").encode())
        status, headers, body = _read_response(sock)
        if status == 401:
            challenges = headers.get("www-authenticate", [])
            digest = next((c for c in challenges if c.lower().startswith("digest")), None)
            if not digest:
                return ProbeResult(False, "Cihaz Digest kimlik doğrulama sunmadı.")
            auth = digest_header(user, password, "DESCRIBE", url, digest)
            sock.sendall((base.format(cseq=2) +
                          f"Authorization: {auth}\r\n\r\n").encode())
            status, headers, body = _read_response(sock)
        if status == 200:
            codec = _codec_from_sdp(body)
            return ProbeResult(True, f"RTSP akışı hazır (kodek: {codec or '?'})", codec)
        if status == 401:
            return ProbeResult(False, "RTSP şifresi reddedildi (401).")
        if status == 404:
            return ProbeResult(False, f"Kanal {channel} bulunamadı (404).")
        if status == 503:
            return ProbeResult(False, "Cihaz meşgul / akış sınırı dolu (503).")
        return ProbeResult(False, f"RTSP yanıt kodu: {status}")
    except (OSError, ValueError, ConnectionError) as e:
        return ProbeResult(False, f"RTSP hatası: {e}")
    finally:
        sock.close()
