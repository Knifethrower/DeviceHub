# DeviceHub HTTP API

For agents and tools that would rather speak HTTP than run the `devhub` command. The CLI (`devhub.py`, standard
library only) is the reference client: everything here is what it sends.

## Basics

- Server: `http://127.0.0.1:8765` (`DEVHUB_PORT` changes the port). It listens on loopback only and refuses requests
  whose `Host` header is not `127.0.0.1`, `localhost` or `::1`.
- **Token**: every `/api/...` and `/shots/...` call except `/api/ping` needs the header `X-DevHub-Token: <token>`. The token is the
  content of `data/token` (created on first start, readable only by the account that runs DeviceHub).
- Bodies are JSON objects; answers are JSON with `"ok": true|false` and, on failure, `"error"` or `"msg"`.
  HTTP 401 = wrong token, 403 = not allowed for this caller, 404 = unknown device/endpoint.
- **Identity** goes in the body of every call:
  ```json
  {"client": "cli", "session": "x-my-agent-1234", "name": "Godot port test"}
  ```
  `session` is a stable id for one agent conversation (Claude Code's `CLAUDE_CODE_SESSION_ID`, or any id of your
  own; the CLI sends `x-` + `DEVHUB_SESSION`). `name` is what the user sees. Without a session only the name
  identifies you.
- **Notices**: answers to calls with an identity may carry `"notices": ["[DeviceHub] ..."]`: lock checks, approvals,
  offers. Show them to the agent and act on them.
- Calls with `"client": "ui"` are the user's (approve, settings, Away mode). Agents must not send it.

## Locks

| Call | Body (besides identity) | Answer |
|---|---|---|
| `POST /api/lock/acquire` | `dev`, `purpose`, `ttl` (`"45m"`), `wait` (bool), `need`, `watch`, `release_on_exit` (bool) | see below |
| `POST /api/lock/renew` | `dev`, `ttl`, `note`, `watch` | `ok`, `msg`; `status: "hold_limit"` when refused by the hold limit |
| `POST /api/lock/release` | `dev`, `dirty` (reason, optional) | `ok`, `msg` |
| `POST /api/lock/ask` | `dev`, `text` | asks the holder whether it still needs the device |
| `POST /api/whoami` | — | `caller`, `holds` |

`acquire` answers `ok: true` when you hold the lock. Otherwise `status` says why:

- `approval`: the user was asked; `deadline` (epoch seconds) is when no answer counts as yes. Repeat the same call
  (the CLI polls every 3 s with `"wait": true`).
- `busy`: someone holds it. With `"wait": true` the answer is `queued` (or `offline_wait` for an offline device):
  keep polling.
- `move` + `alt`: a free device would do; repeat the call with `"dev": alt` (send `need` to stay put).
- `offer`: a device just came online for you. Take it with a new acquire, or keep waiting.
- `denied`, `offline`, `out_of_service`: stop. Use another device or tell the user.

## Using a device

`POST /api/authorize` with `dev`, `ro` (bool) and `cmd` (for the log) returns the shared connection:

```json
{"ok": true, "conn": {"socket": "/tmp/rg35xx.sock", "target": "root@192.168.1.50", "port": 22,
 "known_hosts": "...", "ports_dir": "/roms/ports", "smb": "...", "master": "up"}}
```

Run `ssh -S <socket> -o BatchMode=yes -o UserKnownHostsFile=<known_hosts> -p <port> <target> '<command>'` on the
DeviceHub machine (inside WSL on Windows). A write (`ro: false`) is refused unless you hold the lock. Afterwards
`POST /api/activity` with `dev` marks the lock as in use. To show output in the user's Console view, send
`POST /api/devices/<dev>/console/append` with `lines: [["out", "text"], ["err", "text"], ...]`.

**Files**: `PUT /api/devices/<dev>/file?path=/abs/path` (raw body, lock needed) copies a file; `GET` the same URL
reads one. With *Send files over HTTP* on, the device downloads it from DeviceHub (SHA-1 checked). A port zip,
`PortMaster.zip` or runtime `.squashfs` is never copied this way: upload it and install it (below), so the device's
own PortMaster installs it.

**File manager** (the dashboard's Files dialog): `GET /api/devices/<dev>/fs?path=/abs/path` lists a folder without a
lock (default: the ports folder; `~`: the SSH user's home): `{"path", "writable", "entries": [{"name", "type":
"d"|"f"|"l"|"o", "dir", "size", "mtime", "mode"}], "total", "places", "lock"}`. `POST /api/devices/<dev>/fs` changes
files, with the lock: `op` is `mkdir` or `newfile` (`path`), `rename` (`path`, `to` in the same folder), `chmod`
(`path`, `mode` like `755` or `+x`), `rm` (`paths`), or `mv`/`cp` (`paths`, `to` = a folder, `overwrite`). From the
dashboard (`client: "ui"`) no lock is needed, but a device a session holds answers `{"ok": false, "status": "busy"}`
until the request is repeated with `force: true`. The page uploads with `PUT .../file` and the header
`X-DevHub-Client: ui` (`&force=1` likewise); `GET .../file?...&dl=1` adds a download file name. A replaced file keeps
its permissions.

## FPS

| Call | Body | Notes |
|---|---|---|
| `GET /api/devices/<dev>/fps?n=120` | — | live readings (starts the device's monitor; it stops ~20 s after the last call) |
| `POST /api/devices/<dev>/fps/session` | `action`: `start` (+ `label`) / `stop` | tracked session; `stop` returns the summary |
| `GET /api/fps/sessions?dev=<dev>` | — | sessions, newest first (recording ones first) |
| `GET /api/fps/sessions/<id>[?csv=1]` | — | a session's summary and per-second samples, or CSV |
| `GET /api/devices/<dev>/fps/preload` | — | the `LD_PRELOAD` value that counts frames in a program you launch |
| `POST /api/devices/<dev>/fps/hook` | `on`: bool | count frames in ports launched from PortMaster (lock needed) |

A reading: `{"t": ms, "fps", "worst" (ms), "src", "kind": "app"|"flip"|"idle", "flip": [...], "apps": [...]}`.
`fps` is the busiest program counted inside (`kind: app`), else the busiest screen-flip source.

## Device jobs

| Call | Body | Notes |
|---|---|---|
| `POST /api/devices/<dev>/upload?name=<file>` | raw file bytes | stages a port zip, `PortMaster.zip`, theme or squashfs; returns `upload` id and `info.kind` (none for a port, `portmaster`, `theme`, `squashfs`) |
| `POST /api/devices/<dev>/install` | `upload` or `url`, `refresh` | lock needed; returns `job` |
| `GET /api/jobs/<job>?lines=60` | — | progress and log of a job |
| `POST /api/devices/<dev>/frontend` | `action`: `start`/`stop`/`restart`/`status` | lock needed for changes |
| `POST /api/devices/<dev>/screenshot` | — | returns `file`; fetch it from `GET /shots/<file>` |
| `GET /api/devices/<dev>/ports` | — | installed ports (read-only) |
| `POST /api/devices/<dev>/input/start` | — | plugs in the virtual gamepad and keyboard (lock needed; aarch64 + `/dev/uinput`) |
| `POST /api/devices/<dev>/input/event` | `ev`: a list of events | lock needed; see below |
| `POST /api/devices/<dev>/input/status` | — | `running` (bool) |
| `POST /api/devices/<dev>/input/stop` | — | unplugs them (releasing the device does too) |
| `GET /api/state` | — | everything the dashboard shows: devices, locks, queues, sessions |
| `GET /api/events?dev=<dev>&n=30&since=<epoch>` | — | recent events |

Input events are lists: `[2, code, 1|0]` a gamepad button down/up (Linux codes: `0x130` A/south, `0x131` B/east,
`0x133` X, `0x134` Y, `0x136`/`0x137` L1/R1, `0x138`/`0x139` L2/R2, `0x13a` Select, `0x13b` Start, `0x13c` Guide),
`[3, axis, value]` a gamepad axis (raw `ABS_*` codes: the d-pad is `16` = ABS_HAT0X and `17` = ABS_HAT0Y, -1/0/1;
sticks `0`/`1`/`3`/`4`, ±32767), `[1, code, 1|0]` a keyboard key (`KEY_*` codes), `[4]` a sync. Each call is sent
as one batch and followed by a sync. Hold a button for 150-200 ms between its down and up events: programs that
poll once a frame miss shorter presses.

The user-only endpoints (`/api/settings`, `/api/lock/approve`, device add/edit/delete, passwords, shutdown, out of
service) answer 403 to agents or are meant for the web UI; leave them alone.
