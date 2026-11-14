"""rtsp.probe: sahte RTSP sunucusuna karşı Digest ile DESCRIBE."""
import hashlib
import socketserver
import threading

from nvrizleme import rtsp

USER, PASSWORD, REALM, NONCE = "admin", "p@ss:w#rd", "Login to X", "n0nce"
SDP = "v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\n"


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        buf = b""
        while True:
            while b"\r\n\r\n" not in buf:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head, _, buf = buf.partition(b"\r\n\r\n")
            head = head.decode()
            uri = head.split()[1]
            cseq = [l for l in head.split("\r\n") if l.startswith("CSeq")][0]
            good = md5(f"{md5(f'{USER}:{REALM}:{PASSWORD}')}:{NONCE}:{md5(f'DESCRIBE:{uri}')}")
            if f'response="{good}"' in head:
                out = f"RTSP/1.0 200 OK\r\n{cseq}\r\nContent-Length: {len(SDP)}\r\n\r\n{SDP}"
            else:
                out = (f"RTSP/1.0 401 Unauthorized\r\n{cseq}\r\n"
                       f'WWW-Authenticate: Digest realm="{REALM}", nonce="{NONCE}"\r\n\r\n')
            self.request.sendall(out.encode())


def test_probe():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    ok = rtsp.probe("127.0.0.1", USER, PASSWORD, 1, rtsp.SUB, port, 5)
    assert ok.ok and ok.video_codec == "H265"
    bad = rtsp.probe("127.0.0.1", USER, "yanlis", 1, rtsp.SUB, port, 5)
    assert not bad.ok and "401" in bad.message
    srv.shutdown()
