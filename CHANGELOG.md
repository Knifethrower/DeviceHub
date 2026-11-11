# Changelog

Versions follow [semantic versioning](https://semver.org/): a new feature raises the middle number, a fix the last
one. The version is `VERSION` in `devhubd.py` (the dashboard, the API and the log show it); each release is a git
tag `vX.Y.Z` on its commit. When you change DeviceHub: raise `VERSION`, add an entry here, tag the commit.

Releases before 1.8.0 were tagged afterwards, on the commit each one ended with; their code still says 1.0.

## 1.13.1 (2026-11-10)
- The device editor says why a save failed, in a red line above Save, and highlights the field it is about
  (duplicate id, missing IP address, bad user name, no CFW picked, ...). Messages used to appear behind the open
  window, so a refused save looked like nothing happened. Messages now show above every open window.

## 1.13.0 (2026-11-09)
- Hold limit: an agent that has held a device for 60 minutes (setting, 0 = off) can't extend its lease while others
  wait, so one long session can't starve the line. It keeps the lock until it releases; a running `--watch` job and
  locks taken in the web UI are exempt.
- `acquire --watch NAME --release-on-exit`: the lock is released as soon as the job ends and the device is clean,
  instead of asking the holder. Ideas from issue #7.

## 1.12.0 (2026-11-08)
- Add card profile starts empty and asks for the card's CFW. Login, folders, front-end commands and the name then come
  from that CFW alone, instead of a guessed CFW with leftovers from an earlier pick. Saving needs a CFW on every profile.

## 1.11.3 (2026-11-08)
- Card profiles: when a device first gets profiles, only one with the CFW the device already had inherits its saved
  host key and password. The first row used to inherit them whatever it was, so a profile for a new card was refused
  with a host key mismatch and could never learn its key. The mismatch message now names the way out.

## 1.11.2 (2026-11-08)
- A device that is on the network but refuses SSH shows as "on network, SSH off" instead of "offline".

## 1.11.1 (2026-11-08)
- A `devhub` command that finds the hub down no longer starts a loose copy when DeviceHub is installed as a systemd
  service: it waits for a restarting service, else starts the service. A loose copy took the port and kept the
  service from coming back.

## 1.11.0 (2026-11-07)
- Locked devices stay awake: while a lock is held (or its holder is being asked whether it still needs the device),
  muOS doesn't dim the screen or sleep (a process from its idle watch list) and Knulli's battery-saver is paused (a
  pause file). It ends when the lock does, or by itself 10 minutes after DeviceHub stops renewing it.

## 1.10.0 (2026-11-03)
- Files: a file manager on each device card. Browse folders, upload (also by dropping files), download, edit text
  files, new folder/file, rename, permissions, copy/cut and paste, delete. Listing needs no lock; changes ask you first
  when an agent holds the device, and sessions need the lock (`GET`/`POST /api/devices/<dev>/fs`, see docs/API.md).
- `PUT /api/devices/<dev>/file` (`devhub push`) keeps the permissions of a file it replaces: an edited or re-pushed
  launcher stays executable. On a non-root device (dArkOS) it uses `sudo -n` when the folder or the file isn't the
  user's (a port PortMaster installed as root), keeping the file's owner; `GET` (`devhub pull`, Edit, Download) reads
  a file the user can't with `sudo -n` too.
- tests/files_smoke.sh: the file manager API against the fake device.

## 1.9.0 (2026-11-02)
- Server image: starts again (numpy is optional, with plain-Python fallbacks; the image installs numpy and Pillow),
  and ships the device tools (fps/, stream/, helper/, inject_script2.txt): FPS, live view and remote input work.
- Live view: works without `ip` (the server image); a start that fails says why instead of "internal error".
- Live view on every device: without a Wayland compositor it's `dhcap` (static, aarch64 and armhf: no libraries on the
  device), which picks the DRM plane or /dev/fb0 that shows the screen like screenshots do (GPU-drawn buffers through
  their dma-buf), follows a switch, and sends RGB565 only when the picture changed. dhstream's DRM route failed on
  AmberELEC, Knulli, spruce and dArkOS (no libwayland, an empty plane, GPU buffers), and leaked a GEM handle per
  frame. A start that sends nothing shows the device's streamer log.
- `devhub push` to a non-root user (dArkOS) left a root-owned file when the folder above was read-only (/tmp).
- Remote input on a non-root device (dArkOS): the helper runs with sudo (/dev/uinput is root's), and it and the live
  view streamer are uploaded as the user's own files (written as root they stayed non-executable: input never
  started); stopping a stale helper no longer matches its own shell.
- Live view: stopping it on a non-root device (dArkOS) left the streamer running (it runs with sudo), still sending to a
  port the next live view would get; the receiver takes one sender: its device's address, or (NAT, adb forwarding) the only other one after 1.5 s.
- Beacon autostart is per device and off by default: switched on the beacon list (per device, or All on/off) and
  sent in the signed answer; on, it only starts on the network it was switched on in. A running beacon stops when
  the device joins another network. SSH off: the beacon starts it for this boot the way the CFW's menu does.
- Beacon in a Flatpak (RetroDECK on a Steam Deck): finds its address without `ip`/`ifconfig` (/proc/net/fib_trie), reports
  PortMaster's folders as the host sees them (/var/data is ~/.var/app/<app>/data), has no autostart, and stops when the
  app quits (left running it kept the sandbox, and Steam's "game running", alive).
- muOS: a front-end stop no longer leaves the menu running (another copy per stop/start). Devices added earlier
  get fixed preset fields at start (`PRESET_OLD`). New spruce preset.
- Installs: harbourmaster gets `<name>.md5?md5=<hex>` (the .md5 file is served too), so every PortMaster version
  checks the download.
- Proxied wiki/website pages run sandboxed (their own origin) and only the hub's page may frame them; the page
  takes download messages only from that frame. The wiki's download catching works (its regexes were escaped twice).
- Remote input needs the lock for agents; `devhub fps` reports why the monitor didn't start, and exits 1 with no
  readings.
- Logs: `DEVHUB_LOG_STDERR=1` (set in the image) also writes the log to stderr; a dropped browser connection no
  longer prints a traceback.
- Tests: the smoke tests use free ports and never the caller's hub.

## 1.8.0 (2026-10-31)
- Console: *Save* downloads the whole log file from the device (a port's log from Installed ports, or any followed
  file).
- Version numbers and this changelog.

## 1.7.0 (2026-10-31)
- FPS: live frame rate per device (card button, `devhub fps`) from screen flips (any program, no setup) and inside
  programs (LD_PRELOAD counter, optionally for every port launched from PortMaster); tracked sessions with average,
  1% low, minimum, worst frame, graph and CSV.
- Knulli's Bluetooth agent no longer counts as a running app.

## 1.6.2 (2026-10-31)
- Installs: harbourmaster's exit code is read even when its last line has no newline (a finished install was
  reported as failed).

## 1.6.1 (2026-10-31)
- Installs: harbourmaster's temporary folder is on the SD card, not in `/tmp` (RAM), so big zips fit.
- Installs: a crash harbourmaster catches (and still exits 0 after) is reported as a failure.

## 1.6.0 (2026-10-31)
- Port installs with *Send files over HTTP* on: harbourmaster downloads the zip itself (md5 checked).
- `devhub push` refuses port zips, `PortMaster.zip` and runtimes (use `devhub install`; `--force` to copy anyway).

## 1.5.0 (2026-10-31)
- *Send files over HTTP* covers every file sent to a device (installs, runtimes, `devhub push`, remote agents'
  pushes), with an SSH fallback; under WSL a Windows-side server (`winserve.py`) makes it reachable.

## 1.4.0 (2026-10-31, pull request #3 by Dia2809)
- Live screen streaming, remote input, and a PortMaster catalog and wiki browser.

## 1.3.1 (2026-10-30)
- Shut down works on dArkOS (sudo there keeps the user's PATH, which has no `/sbin`); a failed shutdown is
  reported.

## 1.3.0 (2026-10-30)
- Your own CFW presets in `data/presets.json`, with extra Console log sources.

## 1.2.0 (2026-10-29, pull request #2 by kloptops)
- Remote agents: per-agent tokens; `devhub run`, `push` and `pull` through the hub.
- muOS preset: loads `func.sh`, where `FRONTEND` is defined.

## 1.1.0 (2026-10-29, pull request #1 by kloptops)
- Server mode: one hub on a LAN server; handhelds announce themselves with the DeviceHub Beacon port.
- Install on all: devices fetch the zip from DeviceHub over HTTP; `PortMaster.zip` installs as a PortMaster update.
- muOS preset: front end stop/start with `FRONTEND`; `frontend.sh` is the front-end process.

## 1.0.1 (2026-10-28)
- The laptop preset is no longer part of the public code.

## 1.0.0 (2026-10-28)
- First public release: device locks with approval, queues, shared SSH connections, dashboard, installs through
  PortMaster, screenshots, Console, Claude Code hooks and an agent guide. Linux and Windows (WSL); 0BSD.
