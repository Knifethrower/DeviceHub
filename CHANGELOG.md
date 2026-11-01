# Changelog

Versions follow [semantic versioning](https://semver.org/): a new feature raises the middle number, a fix the last
one. The version is `VERSION` in `devhubd.py` (the dashboard, the API and the log show it); each release is a git
tag `vX.Y.Z` on its commit. When you change DeviceHub: raise `VERSION`, add an entry here, tag the commit.

Releases before 1.8.0 were tagged afterwards, on the commit each one ended with; their code still says 1.0.

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
