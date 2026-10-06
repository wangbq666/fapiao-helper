# -*- coding: utf-8 -*-
"""本地 HTTP CONNECT 代理: github.com 固定走 140.82.112.3, 其余按 DNS 直连"""
import socket
import socketserver
import struct
import threading

LISTEN = ("127.0.0.1", 7899)
OVERRIDE = {"github.com": "140.82.114.3"}
TIMEOUT = 20


def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class H(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline()
            if not line:
                return
            parts = line.decode("latin1").split()
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]
            # 读完请求头
            while True:
                h = self.rfile.readline()
                if not h or h in (b"\r\n", b"\n"):
                    break
            host, _, port = target.partition(":")
            port = int(port or 443)
            ip = OVERRIDE.get(host) or socket.gethostbyname(host)
            up = socket.create_connection((ip, port), timeout=TIMEOUT)
            up.settimeout(None)
            if method == "CONNECT":
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
            else:
                # 非 CONNECT: 透传首行和头(极少用)
                up.sendall(line)
            self.connection.settimeout(None)
            t = threading.Thread(target=pipe, args=(up, self.connection), daemon=True)
            t.start()
            pipe(self.connection, up)
        except Exception:
            pass


class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print("proxy on %s:%d -> github.com=%s" % (LISTEN[0], LISTEN[1],
                                               OVERRIDE["github.com"]))
    Srv(LISTEN, H).serve_forever()
