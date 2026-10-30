#!/usr/bin/env python3
"""DeviceHub's file server on the Windows side of WSL (started by devhubd.py; Windows Python).

Under WSL's default NAT networking the handhelds can't reach a server inside WSL, so files sent over HTTP are
served from Windows, at this PC's LAN address. Only /dl/<token>/<name> is served, for the files listed in the
manifest DeviceHub keeps up to date; the server stops by itself once the manifest has been empty for a minute.

  python winserve.py PORT MANIFEST     serve (prints LISTENING once it is)
  python winserve.py --ip-for HOST     this PC's address on the way to HOST
"""
import http.server, json, os, re, socket, sys, threading, time, urllib.parse

if sys.argv[1:2] == ["--ip-for"]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((sys.argv[2], 9))
        print(s.getsockname()[0])
    sys.exit(0)

PORT, MANIFEST = int(sys.argv[1]), sys.argv[2]
DL = re.compile(r"^/dl/([0-9a-f]{32})/([^/?]+)$")


def entries():
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        m = DL.match(urllib.parse.urlsplit(self.path).path)
        e = entries().get(m.group(1)) if m else None
        if not e or urllib.parse.unquote(m.group(2)) != e["name"]:
            self.send_error(404)
            return
        try:
            f = open(e["path"], "rb")
        except OSError:
            self.send_error(404)
            return
        with f:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.end_headers()
            try:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def idle_exit(srv):
    idle = 0
    while True:
        time.sleep(5)
        idle = 0 if entries() else idle + 5
        if idle >= 60:
            srv.shutdown()
            return


srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
srv.daemon_threads = True
threading.Thread(target=idle_exit, args=(srv,), daemon=True).start()
print("LISTENING", flush=True)
srv.serve_forever()
