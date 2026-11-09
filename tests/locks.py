#!/usr/bin/env python3
"""Lock rules that need no device: the hold limit and --release-on-exit. Run on Linux/WSL: python3 tests/locks.py"""
import os, sys, tempfile

os.environ["DEVHUB_DATA"] = tempfile.mkdtemp()
os.environ.setdefault("DEVHUB_PLATFORM", "linux")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import devhubd as h

DEV = "t1"
A = {"session": "a", "name": "agent-a"}
B = {"session": "b", "name": "agent-b"}
checks = 0


def expect(what, got, want):
    global checks
    checks += 1
    if got != want:
        sys.exit("FAIL: %s: got %r, want %r" % (what, got, want))


def setup(held_min, waiter=True, watch="", running=None):
    n = h.now()
    h.H.settings = dict(h.DEFAULT_SETTINGS)
    d = dict(h.DEVICE_DEFAULTS, id=DEV, name="Test", host="127.0.0.1", fe_proc="")
    h.H.devices = {DEV: d}
    h.H.locks = {DEV: {"holder": dict(A), "purpose": "x", "since": n - held_min * 60, "lease_until": n + 60, "ttl": 1800,
                       "watch": watch, "watch_seen": False, "state": "held", "check": None, "stale_since": None,
                       "last_activity": n, "renewals": 0, "approved": False}}
    h.H.queues = {DEV: [{"session": "b", "name": "agent-b", "purpose": "y", "need": "", "at": n, "seen": n}]} if waiter else {}
    h.rt(DEV)["watch"] = (running, n, watch) if watch and running is not None else None


def renew(c=A):
    return h.do_renew(DEV, dict(c), {"ttl": "30m"})


setup(61)
r = renew()
expect("over the limit, someone waiting: refused", (r["ok"], r["status"]), (False, "hold_limit"))
expect("the lock stays with the holder", h.H.locks[DEV]["holder"]["name"], "agent-a")

setup(59)
expect("under the limit: renewed", renew()["status"], "renewed")

setup(61, waiter=False)
expect("nobody waiting: renewed", renew()["status"], "renewed")

setup(61, watch="game", running=1)
expect("watched job running: renewed", renew()["status"], "renewed")

setup(61, watch="game", running=0)
expect("watched job ended: refused", renew()["status"], "hold_limit")

setup(61)
h.H.settings["hold_limit_min"] = 0
expect("limit off: renewed", renew()["status"], "renewed")

setup(61)
expect("user in the web UI: renewed", renew(dict(A, ui=True))["status"], "renewed")

setup(61)
h.H.locks[DEV]["holder"]["ui"] = True
expect("lock taken in the web UI: renewed", renew(dict(A, ui=True))["status"], "renewed")

setup(61)
h.H.queues[DEV][0].update(session="a", name="agent-a")
expect("only the holder itself queued: renewed", renew()["status"], "renewed")


def job_ended(release, seen, app=None):
    """The watched job is gone from the latest status; returns the lock after a tick."""
    setup(5, waiter=False, watch="game", running=0)
    h.H.locks[DEV].update(watch_release=release, watch_seen=seen)
    h.rt(DEV).update(status={"app": app}, status_at=h.now())
    h.tick()
    return h.H.locks.get(DEV)


expect("job ended, clean, --release-on-exit: released", job_ended(True, True), None)
expect("job ended without the flag: the holder is asked", job_ended(False, True)["state"], "check")
expect("job ended, app left open: the holder is asked", job_ended(True, True, {"name": "x", "rss": 1})["state"], "check")
expect("job never seen running: the holder is asked, no release", job_ended(True, False)["state"], "check")

print("PASS (%d checks)" % checks)
