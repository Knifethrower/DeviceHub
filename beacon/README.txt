DeviceHub Beacon
================

Run "DeviceHub Beacon" from Ports once. It tells your DeviceHub server that this handheld is on the network, and
where; DeviceHub then connects to it over SSH by itself.

- The first time, approve the device on DeviceHub's web page (unless auto-approve is on). DeviceHub then adds its
  own SSH key to this device, so it never needs the device's password.
- SSH must be switched on in the CFW's settings.
- With autostart on (the default when you downloaded it), the port also installs a boot hook, so the device
  announces itself after every boot and every IP change without you running anything:
    Knulli / Batocera   a marked block in /userdata/system/custom.sh
    ROCKNIX / JELOS /   a systemd unit in /storage/.config/system.d
    AmberELEC
    muOS                MUOS/init/devicehub-beacon.sh (switch on Configuration > Custom > User Init Scripts)
    ArkOS / dArkOS      a systemd unit in /etc/systemd/system
- Remove it: sh devicehub/beacon autostart remove, then delete the port.
- What it did last: devicehub/last-run.txt; the background beacon logs to /tmp/devicehub-beacon.log.

beacon.conf holds the key that lets a device announce itself to your hub: don't share this port.
