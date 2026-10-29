#!/bin/bash
# DeviceHub Beacon: announces this handheld to your DeviceHub server, which then connects to it over SSH.
# Run it once from Ports; with autostart on it also installs a boot hook, so later boots need nothing.

XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
if [ -d "/opt/system/Tools/PortMaster/" ]; then
  controlfolder="/opt/system/Tools/PortMaster"
elif [ -d "/opt/tools/PortMaster/" ]; then
  controlfolder="/opt/tools/PortMaster"
elif [ -d "$XDG_DATA_HOME/PortMaster/" ]; then
  controlfolder="$XDG_DATA_HOME/PortMaster"
else
  controlfolder="/roms/ports/PortMaster"
fi

source $controlfolder/control.txt
[ -f "${controlfolder}/mod_${CFW_NAME}.txt" ] && source "${controlfolder}/mod_${CFW_NAME}.txt"

GAMEDIR="/$directory/ports/devicehub"
cd "$GAMEDIR" || exit 1

# what PortMaster knows about this device, for the background beacon (which runs without PortMaster)
{
  for v in CFW_NAME CFW_VERSION DEVICE_NAME DEVICE_CPU DEVICE_RAM DISPLAY_WIDTH DISPLAY_HEIGHT DEVICE_ARCH; do
    eval "x=\${$v}"
    printf "%s='%s'\n" "$v" "$(printf '%s' "$x" | tr -d "'\r\n")"
  done
  # where PortMaster and the ports really are on this device (installs go there)
  printf "PM_DIR='%s'\nPORTS_DIR='%s'\n" "$controlfolder" "/$directory/ports"
} > "$GAMEDIR/device.env"

printf "\033c" > "$CUR_TTY" 2>/dev/null
CUR_TTY="$CUR_TTY" sh "$GAMEDIR/beacon" port 2>&1 | tee "$GAMEDIR/last-run.txt"
sleep 6
printf "\033c" > "$CUR_TTY" 2>/dev/null
