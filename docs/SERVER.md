# DeviceHub server mode

Run one DeviceHub on a LAN server (in a container) instead of on your PC. Handhelds announce themselves to it with
the **DeviceHub Beacon** port, and it connects back to them over SSH with its own key: no typing in IPs, no device
passwords. Everything else (locks, approvals, queues, installs, screenshots, the console) works as before.

## Run it

Copy `server/compose.yaml` into its own folder (a stacks folder is fine): it builds the image from the git repo,
so no checkout is needed there.

```bash
echo "DEVHUB_PASSWORD=choose-one" > .env
echo "DEVHUB_PUBLIC_URL=http://192.168.1.10:8765" >> .env   # this server's LAN address
docker compose up -d --build
```

Open the page, log in, and press **Add device > Download devicehub.zip**. Without Docker: `DEVHUB_PASSWORD=… python3 devhubsrv.py`.

| Variable | Meaning |
|---|---|
| `DEVHUB_PASSWORD` | the web page's login (required, unless a sign-in is in front: see below) |
| `DEVHUB_PUBLIC_URL` | the URL beacons call (default: the address your browser used when downloading the port) |
| `DEVHUB_HUB_NAME` | the hub's name on the device's screen and in its SSH key comment |
| `DEVHUB_BIND`, `DEVHUB_PORT`, `DEVHUB_DATA` | listen address (0.0.0.0), port (8765), data folder |
| `DEVHUB_SSO_*` | a sign-in in front of the hub instead of the password: see [SSO.md](SSO.md) |
| `DEVHUB_LOG_STDERR` | `1` (the image's default): the log (`data/devhubd.log`) also goes to stderr, so `docker logs` shows it |

## Adding a handheld

1. Switch SSH on in the CFW's settings.
2. Put `devicehub.zip` in PortMaster's `autoinstall` folder on the device (or install it with PortMaster) and run **DeviceHub Beacon** from Ports.
3. Approve it on the hub's page (or switch on *Settings > Beacons: new handhelds join without asking*).
4. On its next announce (within 10 s) the beacon adds the hub's SSH key to the device's `authorized_keys`, and the
   device appears as a card, filled in from PortMaster's own device info (CFW, name, screen, RAM) and the CFW preset.

Once run, the beacon keeps announcing (every minute, on an IP change, after a resume) until the device joins another
network (another Wi-Fi name, or another router where there is no Wi-Fi name); then it stops. If SSH is off, it starts
it for this boot the way the CFW's own menu does, without changing any setting.

**Autostart** is off by default and switched per device: on the page (the beacon list: per device, or *All on* /
*All off*; the choice goes to the device in the signed answer to its next announce) or on the device
(`sh devicehub/beacon autostart on|off`). It is kept in `devicehub/autostart`. On, a boot hook starts the beacon after
every boot, but only on the network it was switched on in (`devicehub/autostart.net`: Wi-Fi name and router): Knulli/
Batocera `custom.sh`, ROCKNIX/JELOS/AmberELEC a unit in `/storage/.config/system.d`, muOS `MUOS/init` (User Init
Scripts on), ArkOS/dArkOS a systemd unit. spruce has no boot hook yet (`autostart=none`).

If the key can't be written (some CFWs reset `.ssh`), the card says so and you enter the device's password instead
(Edit > Password), as in normal DeviceHub.

## The hub never goes looking

Local DeviceHub probes every device's SSH port every 10 s and connects whenever it answers. The server doesn't: it
only reaches out to a device that announced itself in the last 2½ minutes (the beacon re-announces every minute), or
for one minute after you press **Connect** on its card (how devices added by hand get connected). A connection that
is already open stays while it lives. So a device is on the hub only while its beacon runs: from its boot hook, or
because you ran the port since it booted. When it stops announcing, its card goes offline and the hub leaves it alone.
*Settings > Poll devices* brings back local DeviceHub's behaviour (probe every device, connect whenever it answers).

## The protocol

- `GET /api/beacon/hello` → `nonce=<hex>` (one use, 2 minutes).
- `POST /api/beacon`: `key=value` lines (`v`, `nonce`, `id`, `challenge`, `name`, `cfw`, `cfw_version`, `ip`, `mac`,
  `user`, `port`, `ssh`, `keyok`, `autostart` (1/0/none), `ssid`, `width`, `height`, `ram`, `hostkey`…), header
  `X-Beacon-Sig: HMAC-SHA256(beacon key, body)`.
- The answer: `key=value` lines (`state` = pending / approved / denied, `dev`, `msg`, `interval`, `pubkey` and the
  user's `autostart` choice (1/0, if any) once approved, the device's `challenge` echoed), ending with `sig=HMAC-SHA256(beacon key, everything before it)`. The
  beacon ignores an answer that doesn't verify, so nobody on the network can hand it an SSH key to install.
- The beacon key is in `data/beacon.key` and in every downloaded port; *Change the beacon key* makes old ports stop
  working. The device id is random, made once per SD card (`devicehub/id`).
- The device's SSH host keys come in the signed announce and are pinned in `data/known_hosts/hub`; when DHCP gives
  it a new IP, the card and the pinned keys follow.

## Access

- The page needs the login; only a logged-in page acts as the user (approving devices and agents' requests, Away
  mode, settings).
- Agents on other machines get their own token from the page (**Agents**), which also gives the message to paste to
  the agent, or a Claude Code skill (`SKILL.md`) with the setup and rules filled in. The agent downloads the `devhub`
  command from the hub (`/devhub.py`) and sets `DEVHUB_URL`, `DEVHUB_TOKEN` and `DEVHUB_NAME`; `devhub run`, `push`
  and `pull` then go through the hub (`/api/devices/<dev>/exec` and `/file`), which runs them over the device's
  shared connection with the usual lock rules. Agent tokens can never act as the user or stop the hub, and can be
  revoked. Agents on the server itself can keep using `data/token`.
- Keep it on your LAN (or tailnet): don't publish it to the internet, except through a sign-in (below).

## Behind a sign-in

The page can use the sign-in you have in front of your other services (Authelia, Authentik, oauth2-proxy...) instead
of its own password: members of one group are logged in, and the port itself then serves only devices and agents.
[SSO.md](SSO.md) has what the proxy must do, DeviceHub's `DEVHUB_SSO_*` settings, and recipes.

## Tests

`tests/server_smoke.sh` runs the whole flow on localhost against a fake SSH device, with the beacon in busybox sh
(no curl, no openssl): `PYTHON=/path/to/python-with-paramiko tests/server_smoke.sh`.
