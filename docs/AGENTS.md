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
   `devhub renew DEV 30m "why"` or `devhub release DEV`.
6. **Leave the device clean**: front end running, your program stopped. `release` is refused while the front end is
   down. `devhub fe DEV restart` fixes that.
7. Never change DeviceHub's settings, approve your own requests, or switch Away mode: those are the user's, in the
   web UI only (the API refuses agents).

## Commands

```
devhub status [DEV]                                   devices, connections, who holds what
devhub acquire DEV "purpose" [--for 45m]              take the lock (exit 1 = busy/refused, the message says why)
devhub acquire DEV "purpose" --wait 20m [--need WHY]  wait in line
devhub acquire DEV "purpose" --watch NAME             lease kept while a process called NAME runs on the device
devhub renew DEV [30m] ["why"]                        still need it
devhub release DEV                                    give it back
devhub release DEV --dirty "why"                      give it back although the front end is down (user is told)

devhub run DEV -- 'command'                           run over the shared connection (lock needed)
devhub run DEV --ro -- 'cat /tmp/log.txt'             read-only look, no lock needed
devhub push DEV local/file /remote/path               copy to the device (lock needed)
devhub pull DEV /remote/path local/file               copy from the device
devhub install DEV game.zip | runtime.squashfs | URL  install with the device's own PortMaster (lock needed)
devhub ports DEV                                      installed ports (read-only)
devhub fe DEV start|stop|restart|status               front end (EmulationStation, ...)
devhub shot DEV out.png                               screenshot of what is on screen
eval "$(devhub env DEV)"; $DH_SSH 'uname -a'          shell variables for your own scripts (lock first)
devhub log [DEV]                                      recent events
devhub whoami                                         who DeviceHub thinks you are, what you hold
```

Exit codes: 0 ok, 1 refused or busy, 2 usage error, 3 DeviceHub not running.

Everything `devhub run` prints also appears live in the device's Console in the web UI, so the user can follow.
Messages starting with `[DeviceHub]` in any command's output are meant for you: lock checks, approvals, offers of
a device that just came online. Act on them.

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
