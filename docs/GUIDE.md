# DeviceHub user guide

Everything the web UI (http://127.0.0.1:8765) does, card by card. For installing see the [README](../README.md); for
agents see [AGENTS.md](AGENTS.md).

## Devices

- **Add a device**: *Add device*. Pick the device type (fills SoC/GPU/RAM/screen) and the CFW (fills user, login
  method, ports folder, front-end commands, SMB share). Changing the IP address later carries the new IP into the
  settings that contain it (the SMB share, in every card profile). Login methods:
  - *Password*: type it once. DeviceHub encrypts it (Windows DPAPI under WSL, openssl with a key in
    `data/secret.key` on Linux) and opens or reopens the shared connection itself whenever the device is online.
    The password is never shown again, never returned by the API, and never stored in clear text; ssh gets it
    through a one-shot FIFO. A rejected password is not retried until you change it or press Retry.
  - *SSH key*: the keys in `~/.ssh` of the account DeviceHub runs as (or a key file path).
  - *Manual*: you open the connection yourself (*Copy open command*).
- **Status**: online/offline, shared connection, front end, the app running, memory, temperature, battery.
  ROCKNIX devices also show their GPU driver (libmali, Panfrost or Adreno).
- **Battery** tile: charge %, and plugged in / charging / full / on battery; voltage and current in its tooltip.
- **Device type / Retro Catalog** (Settings > *Device types from Retro Catalog*, off by default): the editor's *Device
  type* suggests the handhelds listed on retrocatalog.com; picking one fills SoC, GPU, RAM and screen from its spec
  page and links the device to it. The list is fetched at most weekly and a model's page only when picked (cached
  30 days in `data/retrocatalog.json`).
- **SD card profiles** (Edit > SD card > *Add card profile*): one device with several cards, each with its own CFW,
  login, password, folders and front-end commands, and its own SSH host key file. DeviceHub switches by itself when
  the device answers with another profile's host key (a card swap); a card it has never seen is flagged, and
  picking its profile once lets DeviceHub learn its key.
- **Out of service** (More… > *Take out of service*, optional reason): DeviceHub keeps connecting, polling and
  watching the battery, but no agent gets the device: acquires are refused, its queue is dropped and the waiters
  told. A lock someone already holds stays; the holder is asked to finish up. *Put back in service* offers it to
  waiting agents at once.
- **Shut down** (More…): asks first, names a lock holder and a running app.
- **Low battery** (Settings): off the charger, a notification at `battery_alert_pct` (10%), and at
  `battery_shutdown_pct` (3%, two readings in a row) a clean shutdown: front end stopped (it saves its settings and
  game lists), sync, poweroff. A lock on the device is released and its holder told. Per device (Edit > Rules) the
  shutdown can be turned off, e.g. for a laptop.
- **Disconnect** pauses auto-connect for that device until *Connect*.
- **Network services** (dArkOS): brings Wi-Fi services back the way the device's own menu does.

## Locks and approvals

- The card shows holder, purpose, lease, whether the holder was asked and answered, and what the device is doing.
  *I'm using it* takes the lock for you. *Ask if still needed* sends the holder a check now. *Force release* takes
  it away (the holder is told).
- **Order**: locked devices first, then online, offline, and out of service.
- **Approval** (per device, Edit > Rules): *auto* (default) = an agent's request sends you a notification with
  Approve/Deny, and no answer within `approval_timeout_min` (10) counts as yes; *ask* = it waits for your answer;
  *off* = the lock is enough. Renewing a held lock needs no new approval.
- **Request chains**: within `approval_chain_min` (10) after an approval on a device, or after an approved agent's
  session on it ends, the next request is approved at once. A Deny closes the chain; 0 turns chains off.
- **Away mode** (top bar): every request for a free device is approved at once, without a notification. Locks,
  queues and *out of service* still apply. Only the UI can switch it.
- **Lock checks**: a lock is a lease. When it runs out (or a `--watch` job ends), DeviceHub asks the holder whether
  it still needs the device. No answer in time: you get a notification; a stale lock whose holder is idle is
  released if the device is clean (front end running, no app left open). An agent session that ends releases its
  locks.
- **Queues and offers**: agents wait in line for busy devices; a waiting agent that doesn't need that exact device
  is moved to a free one. Devices that come online are offered to waiting agents.
- **Guard** (Claude Code hooks): an agent's raw shell command that names a device (IP, socket, extra match words) is
  blocked while someone else holds that device; `# devhub:ro` marks a read-only look. Per device: enforce / warn /
  off.

## Installing

- **Install port…** (card button, or `devhub install DEV zip`): pick or drop a port zip. DeviceHub checks it is a
  PortMaster port zip (launcher `.sh` at the top, no unsafe paths) and shows title, folders, size and runtimes. Then
  it copies the zip to `<PortMaster>/.devhub-staging/` (SHA-1 checked) and runs the device's own
  `harbourmaster --no-check install <zip>`, exactly what PortMaster's autoinstall does. Afterwards it reloads the
  game list (where the front end can), restarts the front end, or leaves it, as you choose. The UI install takes the
  lock for you while it runs. Uploaded zips are deleted after 30 minutes.
- **Runtimes**: a `.squashfs` file is copied straight into `<PortMaster>/libs/`.
- **From a URL**: the *or install from a URL* field (or `devhub install DEV https://…`) runs
  `harbourmaster --no-check install <url>` on the device (internet needed); a `.zip.md5` URL makes harbourmaster
  check the download.
- **PortMaster itself**: PortMaster's release zip (`PortMaster.zip`) installs like a port, on one device or on all:
  harbourmaster updates PortMaster in place, as its own updater does.
- **Install on all…** (top bar): one zip, uploaded once, installed on every device you tick, in parallel. Each
  device's harbourmaster downloads it from DeviceHub over HTTP (`?md5=` checked) instead of DeviceHub copying it to
  each over SSH: a hub on the LAN (server mode) serves it on its own port, a local one starts a file server on
  `DEVHUB_FILE_PORT` (8766) while the installs run. Settings > *Install on all: each device downloads…* switches back
  to copying (the default under WSL, whose network the devices can't reach). Runtimes (`.squashfs`) are always copied. The list
  says why a device can't take it; devices an agent holds are unticked. One notification sums up the run.
- **Installed ports** (card button, or `devhub ports DEV`): read-only list from each port's `port.json`, plus
  launchers PortMaster does not manage and installed runtimes. The *Log* column opens a port's log files in the
  Console, following them live.

## Watching

- **Console** (card button): agents' `devhub run` commands on that device with their output, the running app's
  stdout/stderr, the port's `log.txt`, front-end launch logs, the PortMaster log, recent `.log` files in `/tmp`, or
  any path you type. Read-only: one short `tail` per second while the dialog is open.
- **Screenshots** (card button): what is on the screen. DeviceHub tries `grim` (Wayland), then ffmpeg's `kmsgrab`,
  then a direct read of the scanned-out DRM plane, then `/dev/fb0`, and rotates portrait panels. *All screenshots*
  lists the newest 30 per device with who took them; open, download or delete them.
- **Notifications**: browser notifications (allow them for the page) with Approve/Deny buttons on approval
  requests; under WSL optionally Windows toasts too. **Notification command** (Settings) runs a shell command for every
  alert with `DEVHUB_TITLE`, `DEVHUB_TEXT`, `DEVHUB_URL` and `DEVHUB_KIND` set, e.g.
  `notify-send "$DEVHUB_TITLE" "$DEVHUB_TEXT"` or a curl to ntfy.sh for your phone.
- The page refreshes its data every 2 s and reloads itself when DeviceHub is updated.

## Laptops and Linux PCs

*Also counts as the front end running* (`fe_alt`, e.g. `lightdm`) makes a normal desktop count as a clean, running
front end (the card says "desktop"); desktop programs never count as an app left running.

## Running it

- **Windows**: double-click **Start DeviceHub.bat** (starts it and opens the dashboard), **Stop DeviceHub.bat**,
  **Restart DeviceHub.bat**, **DeviceHub Status.bat**, and **Restart WSL and DeviceHub.bat** for when DeviceHub runs
  but Windows cannot reach it (WSL's port forwarding stuck; asks first, since it stops everything running in WSL).
- **Linux**: `systemctl start|stop|restart|status devhub`.
- Without the service: `devhub start` / `devhub stop`. Every devhub command starts the server if it isn't running.
- The shared SSH connections survive a restart and are adopted by the next start.
- Environment: `DEVHUB_PORT` (8765), `DEVHUB_DATA` (`data/`), `DEVHUB_PLATFORM` (`wsl` or `linux`, normally
  detected), `DEVHUB_NO_AUTOSTART=1` (don't start the server from the CLI).
- **Legacy lock file**: a device's *lock file* field keeps an older script's lock file in step with DeviceHub's
  lock, both ways.

## Data

Everything lives in `data/` (never commit it): `devices.json`, `state.json` (locks, queues, settings),
`events.jsonl`, `secrets.json` (encrypted passwords), `secret.key` (Linux), `token`, `shots/`, `devhubd.log`.
