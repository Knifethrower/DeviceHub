# DeviceHub: instructions for AI agents

Paste this into your agent's instructions (AGENTS.md, CLAUDE.md, a system prompt, ...), or point the agent at
`devhub agent-guide`, which prints it.

DeviceHub owns the test handhelds on this desk: who may change a device, the shared SSH connection to each one, and
what the user sees on the dashboard (http://127.0.0.1:8765). Use the `devhub` command for every device; never open
your own SSH connection to a device and never ask the user for a device password.

## Who you are

DeviceHub shows the user who holds each device, so it needs a stable name for you.

- **Claude Code** sets `CLAUDE_CODE_SESSION_ID` itself. Run `devhub name "what you are working on"` once.
- **Any other agent**: export two variables in the shell your commands run in, then name yourself:
  ```
  export DEVHUB_SESSION="my-agent-1234"     # stable for this conversation/task, unique per agent
  export DEVHUB_NAME="Godot port test"      # what the user sees
  ```
  If you cannot keep variables between commands, put `--as "<name>"` on every devhub command instead.

## On another machine

A DeviceHub server (on your LAN) works the same from any machine. Its page (Agents) gives each agent a token and the
setup: the `devhub` command downloaded from the hub (`<hub>/devhub.py`, one file, Python 3 only) and three variables
in the environment of every devhub command:

```
DEVHUB_URL=http://<hub>:8765      DEVHUB_TOKEN=dh_...      DEVHUB_NAME="what you are working on"
```

Then `devhub run`, `push` and `pull` go through the hub, which runs them over the device's shared connection (the
same lock rules; everything shows in the device's Console). `devhub env` needs DeviceHub on your own machine.

## The rules

1. **Every change on a device needs its lock**: launching or killing programs, stopping the front end, copying or
   installing files, changing settings. Read-only looks don't (`--ro`).
2. **Ask DeviceHub, not the chat.** You may take any online, free device: `devhub acquire` asks the user for you
   (a notification with Approve/Deny). Depending on the device's setting, no answer within 10 minutes counts as yes,
   or the request waits for the user. Run long waits in the background, or run the same acquire again later.
3. **Offline devices are refused**: ask the user to switch it on. **Out of service** means the user parked it:
   use another device, never work around it.
4. **Busy devices queue**: `--wait 20m`. If another device would do, leave it to DeviceHub: a waiting acquire is
   moved to a free device and prints `NOTE: you now hold <dev>`. From then on, use that device id. If you need that
   exact device, say why: `--need "720x720 layout test"`.
5. **A lock is a lease.** When it runs out DeviceHub asks whether you still need the device. The question shows up
   in the output of your next devhub command (and through the hooks in Claude Code). Answer with
   `devhub renew DEV 30m "why"` or `devhub release DEV`. After a long hold (60 minutes by default), a renew is
   refused while others wait: finish, release, and `acquire --wait` to rejoin at the end of the line.
6. **Ports are installed, not copied.** A port zip, `PortMaster.zip`, a theme or a runtime `.squashfs` goes on with
   `devhub install`: the device's own PortMaster installs it (folders, port.json, game list, permissions; a theme
   into PortMaster's themes folder). `devhub push` is for every other file (configs, data) and refuses port zips and runtimes;
   never unzip a port by hand or drop it into PortMaster's `autoinstall` folder.
7. **Leave the device clean**: front end running, your program stopped. `release` is refused while the front end is
   down. `devhub fe DEV restart` fixes that.
8. Never change DeviceHub's settings, approve your own requests, or switch Away mode: those are the user's, in the
   web UI only (the API refuses agents).

## Commands

```
devhub status [DEV]                                   devices, connections, who holds what
devhub acquire DEV "purpose" [--for 45m]              take the lock (exit 1 = busy/refused, the message says why)
devhub acquire DEV "purpose" --wait 20m [--need WHY]  wait in line
devhub acquire DEV "purpose" --watch NAME             lease kept while a process called NAME runs on the device
devhub acquire DEV "purpose" --watch NAME --release-on-exit   and released when it ends, if the device is clean
devhub renew DEV [30m] ["why"]                        still need it
devhub release DEV                                    give it back
devhub release DEV --dirty "why"                      give it back although the front end is down (user is told)

devhub run DEV -- 'command'                           run over the shared connection (lock needed)
devhub run DEV --ro -- 'cat /tmp/log.txt'             read-only look, no lock needed
devhub push DEV local/file /remote/path               plain file copy (lock needed; not for port zips)
devhub pull DEV /remote/path local/file               copy from the device
devhub install DEV game.zip | runtime.squashfs | URL  install with the device's own PortMaster (lock needed);
devhub install DEV PortMaster.zip                     PortMaster.zip too: a test build of PortMaster itself
devhub install DEV Doom.theme.zip                     a PortMaster theme (a zip with theme.json; named .theme.zip on the device)
devhub ports DEV                                      installed ports (read-only)
devhub fe DEV start|stop|restart|status               front end (EmulationStation, ...)
devhub shot DEV out.png                               screenshot of what is on screen
devhub pad DEV down down a                            press buttons on a virtual gamepad (lock needed; see below)
devhub pad DEV select+y 2s b                          a chord, a wait, another press
devhub pad DEV start | stop | status                  plug the gamepad in / unplug it / is it plugged in
devhub fps DEV [--for 30s]                            live frame rate, one line per second (no lock needed)
devhub fps DEV start "label" / stop / list            tracked FPS session: avg, 1% low, min, worst frame
devhub fps DEV preload                                LD_PRELOAD value that counts frames in your own launches
devhub fps DEV hook on|off                            count frames in ports launched from PortMaster (lock needed;
                                                      switch it off again before you release the device)
eval "$(devhub env DEV)"; $DH_SSH 'uname -a'          shell variables for your own scripts (lock first)
devhub log [DEV]                                      recent events
devhub whoami                                         who DeviceHub thinks you are, what you hold
```

Exit codes: 0 ok, 1 refused or busy, 2 usage error, 3 DeviceHub not running.

Everything `devhub run` prints also appears live in the device's Console in the web UI, so the user can follow.
Messages starting with `[DeviceHub]` in any command's output are meant for you: lock checks, approvals, offers of
a device that just came online. Act on them.

## Driving the screen

`devhub pad` presses buttons for you, and `devhub shot` shows what happened: together they let you work a GUI the
way the tester would (PortMaster's menus, a port's title screen). The first `pad` command plugs a virtual gamepad,
"DeviceHub Gamepad", into the device (aarch64 devices with `/dev/uinput`); `devhub pad DEV stop` or releasing the
device unplugs it.

- **Plug it in before you start the program** (`devhub pad DEV start`): most programs (SDL) look for controllers
  when they start, and don't see one plugged in later.
- **Which button is A depends on the device.** `a b x y` are the Linux codes where an Xbox pad has them, but the
  device's controller database decides what a program calls A: on some devices `a` is the program's B. Press, take
  a screenshot, and check before you go on. A program's log often prints the mapping (PortMaster's `pugwash.txt`:
  "Opened GameController").
- Each press is held 200 ms (`--hold MS`), then a 250 ms pause (`--gap MS`). Shorter presses get missed by programs
  that read the buttons once a frame.
- Buttons: `a b x y l1 r1 l2 r2 l3 r3 select start guide` and the d-pad `up down left right`. `select+y` holds Select,
  presses Y and lets go of both; `down*3` presses three times; `500ms` or `2s` waits.
- Stop the front end first if the program needs the screen to itself (`devhub fe DEV stop`), and start it again
  before you release.

```
devhub fe rg35xx stop
devhub pad rg35xx start
devhub run rg35xx -- 'cd /roms/ports && nohup ./Shovel.sh > /tmp/shovel.log 2>&1 &'
devhub pad rg35xx 5s start a        # wait for the title screen, then Start, A
devhub shot rg35xx title.png
```

## Typical session

```
devhub name "Shovel port: input test"
devhub status
devhub acquire rg35xx "input mapping test, ES stopped" --for 45m
devhub install rg35xx build/shovel.zip
devhub run rg35xx -- 'cd /roms/ports && ./Shovel.sh > /tmp/shovel.log 2>&1 &'
devhub shot rg35xx shot.png
devhub run rg35xx -- 'pkill -x shovel'
devhub fe rg35xx restart
devhub release rg35xx
```
