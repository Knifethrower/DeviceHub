DeviceHub Beacon
================

Run "DeviceHub Beacon" from Ports. It tells your DeviceHub server that this handheld is on the network, and
where; DeviceHub then connects to it over SSH by itself. It keeps running in the background until the device
joins another network (another Wi-Fi name or router), or until it is switched off.

- The first time, approve the device on DeviceHub's web page (unless auto-approve is on). DeviceHub then adds its
  own SSH key to this device, so it never needs the device's password.
- SSH must be on. If it is off, the beacon starts it for this boot, the way the CFW's own menu does (no setting
  is changed); where it can't, it says so.
- Autostart is off unless you switch it on: on DeviceHub's page (per device, or for all), or here with
  sh devicehub/beacon autostart on. Then a boot hook starts the beacon after every boot, but only on the network
  it was switched on in. The choice is kept in devicehub/autostart. The boot hooks:
    Knulli / Batocera   a marked block in /userdata/system/custom.sh
    ROCKNIX / JELOS /   a systemd unit in /storage/.config/system.d
    AmberELEC
    muOS                MUOS/init/devicehub-beacon.sh (switch on Configuration > Custom > User Init Scripts)
    ArkOS / dArkOS      a systemd unit in /etc/systemd/system
    spruce              none yet: run the port after each boot
- Remove it: sh devicehub/beacon autostart off, then delete the port.
- What it did last: devicehub/last-run.txt; the background beacon logs to /tmp/devicehub-beacon.log.

beacon.conf holds the key that lets a device announce itself to your hub: don't share this port.
