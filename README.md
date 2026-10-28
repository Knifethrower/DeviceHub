# DeviceHub

A desk-side hub for the retro handhelds you test ports on, shared between you and your AI coding agents.

DeviceHub keeps one SSH connection open to every handheld, decides who may change which device (with your approval),
and shows it all on a local dashboard. It was built for PortMaster port testing with Claude Code running several
sessions at once, but any agent that can run a shell command can use it.

- **Locks with approval**: an agent takes a device's lock before it changes anything. You approve from a browser
  notification (or let it auto-approve after a while, or switch on Away mode). Locks are leases: idle holders are
  asked whether they still need the device, and stale locks are released when the device is clean.
- **Queues**: busy devices queue; an agent that doesn't need that exact device is moved to a free one, and devices
  that come online are offered to waiting agents.
- **Shared SSH**: OpenSSH ControlMaster connections, opened and reopened by the hub. Passwords are typed once into the
  UI and stored encrypted; agents never see them.
- **Dashboard** (http://127.0.0.1:8765): status of every device (front end, running app, memory, temperature,
  battery, GPU driver), live console with agents' commands and port logs, screenshots of what is really on screen,
  installed ports, SD card profiles, out-of-service, shutdown, low-battery protection.
- **Installs** through the device's own PortMaster (`harbourmaster`): a port zip, a runtime `.squashfs`, or a URL,
  on one device or on all of them at once.
- **Guard** (Claude Code hooks): a raw shell command that names a device someone else holds is blocked.

Tested with Knulli, ROCKNIX, muOS, dArkOS and AmberELEC; presets fill in each one's user, folders and front-end
commands.

## Requirements

- **Windows 10/11 with WSL 2** (Ubuntu or Debian) with systemd turned on, **or Linux** with systemd.
- Python 3.8+ (standard library only), OpenSSH client; on Linux also `openssl` (password encryption).
- Windows: Python for the `devhub` command on the Windows side (Git Bash, PowerShell or cmd).
- Devices reachable over SSH on your network.

## Install

```bash
git clone https://github.com/Knifethrower/DeviceHub.git
cd DeviceHub
sudo setup/install.sh
```

On Windows run those commands inside WSL, in a folder under `/mnt/c/...` so the Windows side can use the same
checkout. The script installs a systemd service that runs as your user and starts with WSL/the machine. If WSL has
no systemd yet it tells you how to turn it on. Without systemd, `./devhub start` runs the server by hand.

Then open http://127.0.0.1:8765, allow notifications, and add your devices (*Add device*: type, CFW, IP, login).
Windows users get double-click launchers: `Start DeviceHub.bat`, `Stop DeviceHub.bat`, `Restart DeviceHub.bat`,
`DeviceHub Status.bat`.

The `devhub` command: `./devhub` (Linux, WSL, Git Bash), `devhub.cmd` (PowerShell, cmd), or `python devhub.py`.
Put the folder on your PATH if you like.

## Hooking up AI agents

**Claude Code**:

```bash
devhub install-hooks
```

This adds DeviceHub's hooks to `~/.claude/settings.json` (or `--settings path/.claude/settings.json` for one project;
`--print` shows the result without writing; `--remove` takes them out). The hooks name sessions on the dashboard,
deliver lock checks and approvals into the conversation, release a session's locks when it ends, and run the guard.
Then add the agent instructions to your `CLAUDE.md`: `devhub agent-guide >> CLAUDE.md`.

**Any other agent** (Codex, Gemini CLI, Aider, your own scripts): give it the instructions from
`devhub agent-guide` (the same text as [docs/AGENTS.md](docs/AGENTS.md)) and have it set `DEVHUB_SESSION` (a stable
id) and `DEVHUB_NAME` (what you see) in its shell. Everything works through the `devhub` command; DeviceHub's
messages for the agent come back in that command's output. Agents that speak HTTP can use the
[HTTP API](docs/API.md) directly.

```
devhub acquire rg35xx "input test" --for 45m      # asks you, then holds the lock
devhub install rg35xx build/game.zip
devhub run rg35xx -- 'cat /proc/meminfo | head -3'
devhub shot rg35xx shot.png
devhub release rg35xx
```

## Security model

- The server listens on 127.0.0.1 only, checks the `Host` header (no DNS rebinding), and every API call needs the
  token in `data/token` (owner-only file permissions). Anything running as your user can read that token: DeviceHub
  coordinates cooperative agents, it is not a sandbox.
- Passwords: Windows DPAPI (tied to your Windows account) under WSL, AES-256 via openssl with a random key in
  `data/secret.key` on Linux. They never leave the server; ssh gets them through a one-shot FIFO.
- Approvals, settings (Away mode, the *Notification command*, which runs a shell command you type) are accepted only
  from calls marked as the web UI's. That keeps well-behaved agents from approving their own requests; like the
  token, it does not stop a program that sets out to pretend to be the UI.
- `data/` holds your devices, passwords, logs and screenshots. Keep it out of git (`.gitignore` does).

## More

- [User guide](docs/GUIDE.md): every feature of the dashboard
- [Agent guide](docs/AGENTS.md): the rules and commands for AI agents
- [HTTP API](docs/API.md)

## License

[0BSD](LICENSE): do whatever you like with it.
