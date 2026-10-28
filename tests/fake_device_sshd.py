#!/usr/bin/env python3
"""Throwaway SSH server for testing DeviceHub: password auth, exec channels run through sh locally.
usage: fake_device_sshd.py PORT PASSWORD_FILE HOSTKEY_FILE   (needs paramiko; the host key is made if missing)"""
import logging, os, socket, subprocess, sys, threading
import paramiko

logging.getLogger("paramiko").setLevel(logging.CRITICAL)    # DeviceHub's TCP probe closes without a banner

PORT = int(sys.argv[1])
PW = open(sys.argv[2]).read().strip()
if not os.path.exists(sys.argv[3]):
    paramiko.RSAKey.generate(2048).write_private_key_file(sys.argv[3])
KEY = paramiko.RSAKey(filename=sys.argv[3])


class Srv(paramiko.ServerInterface):
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == PW else paramiko.AUTH_FAILED

    def check_channel_exec_request(self, chan, command):
        threading.Thread(target=run, args=(chan, command.decode()), daemon=True).start()
        return True


def run(chan, cmd):
    p = subprocess.Popen(["sh", "-c", cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def pump_in():
        try:
            while True:
                b = chan.recv(65536)
                if not b:
                    break
                p.stdin.write(b)
                p.stdin.flush()
        except Exception:
            pass
        try:
            p.stdin.close()
        except Exception:
            pass

    def pump_err():
        for b in iter(lambda: p.stderr.read1(65536), b""):
            chan.sendall_stderr(b)

    threading.Thread(target=pump_in, daemon=True).start()
    te = threading.Thread(target=pump_err, daemon=True)
    te.start()
    for b in iter(lambda: p.stdout.read1(65536), b""):
        chan.sendall(b)
    te.join()
    chan.send_exit_status(p.wait())
    chan.close()


transports = []      # keep references: paramiko channels are weakly referenced
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", PORT))
s.listen(20)
while True:
    conn, _ = s.accept()
    try:      # DeviceHub's reachability probe connects and closes without a banner
        t = paramiko.Transport(conn)
        t.add_server_key(KEY)
        t.start_server(server=Srv())
        transports.append(t)
    except Exception:
        conn.close()
