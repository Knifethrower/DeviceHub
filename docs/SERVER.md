# DeviceHub server mode

Run one DeviceHub on a LAN server (in a container) instead of on your PC. Handhelds announce themselves to it with
the **DeviceHub Beacon** port, and it connects back to them over SSH with its own key: no typing in IPs, no device
passwords. Everything else (locks, approvals, queues, installs, screenshots, the console) works as before.

## Run it

```bash
cd server
echo "DEVHUB_PASSWORD=choose-one" > .env
echo "DEVHUB_PUBLIC_URL=http://192.168.1.10:8765" >> .env   # this server's LAN address
docker compose up -d --build
```

Open the page, log in, and use **Beacon… > Download the beacon port**. Without Docker: `DEVHUB_PASSWORD=… python3 devhubsrv.py`.

| Variable | Meaning |
|---|---|
| `DEVHUB_PASSWORD` | the web page's login (required) |
| `DEVHUB_PUBLIC_URL` | the URL beacons call (default: the address your browser used when downloading the port) |
| `DEVHUB_HUB_NAME` | the hub's name on the device's screen and in its SSH key comment |
| `DEVHUB_BIND`, `DEVHUB_PORT`, `DEVHUB_DATA` | listen address (0.0.0.0), port (8765), data folder |

## Adding a handheld

1. Switch SSH on in the CFW's settings.
2. Copy the beacon port to the device's ports folder (or install the zip with PortMaster) and run **DeviceHub Beacon**.
3. Approve it on the hub's page (or switch on *Settings > Beacons: new handhelds join without asking*).
4. On its next announce (within 10 s) the beacon adds the hub's SSH key to the device's `authorized_keys`, and the
   device appears as a card, filled in from PortMaster's own device info (CFW, name, screen, RAM) and the CFW preset.

With autostart (the default download) the port also installs a boot hook, so the device announces itself after every
boot, IP change and resume, with nothing to run: Knulli/Batocera `custom.sh`, ROCKNIX/JELOS/AmberELEC a unit in
`/storage/.config/system.d`, muOS `MUOS/init` (User Init Scripts on), ArkOS/dArkOS a systemd unit.

If the key can't be written (some CFWs reset `.ssh`), the card says so and you enter the device's password instead
(Edit > Password), as in normal DeviceHub.

## The protocol

- `GET /api/beacon/hello` → `nonce=<hex>` (one use, 2 minutes).
- `POST /api/beacon`: `key=value` lines (`v`, `nonce`, `id`, `challenge`, `name`, `cfw`, `cfw_version`, `ip`, `mac`,
  `user`, `port`, `ssh`, `keyok`, `autostart`, `width`, `height`, `ram`, `hostkey`…), header
  `X-Beacon-Sig: HMAC-SHA256(beacon key, body)`.
- The answer: `key=value` lines (`state` = pending / approved / denied, `dev`, `msg`, `interval`, `pubkey` once
  approved, the device's `challenge` echoed), ending with `sig=HMAC-SHA256(beacon key, everything before it)`. The
  beacon ignores an answer that doesn't verify, so nobody on the network can hand it an SSH key to install.
- The beacon key is in `data/beacon.key` and in every downloaded port; *Change the beacon key* makes old ports stop
  working. The device id is random, made once per SD card (`devicehub/id`).
- The device's SSH host keys come in the signed announce and are pinned in `data/known_hosts/hub`; when DHCP gives
  it a new IP, the card and the pinned keys follow.

## Access

- The page needs the login; only a logged-in page acts as the user (approving devices and agents' requests, Away
  mode, settings).
- Agents use the API token from `data/token` (`X-DevHub-Token`), exactly as with local DeviceHub. They need to run on
  the server for `devhub run` (shared SSH sockets); remote agents are the next step.
- Keep it on your LAN (or tailnet): don't publish it to the internet.

## Tests

`tests/server_smoke.sh` runs the whole flow on localhost against a fake SSH device, with the beacon in busybox sh
(no curl, no openssl): `PYTHON=/path/to/python-with-paramiko tests/server_smoke.sh`.
