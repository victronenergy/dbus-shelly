# Venus driver for Shelly energy meters

This is a VenusOS driver for Gen2 Shelly energy meters.

## Preferred mode of installation
Your GX device needs a static/fixed IP address. The energy meter will push
data to the GX device using the websockets protocol.

## Supported configurations
A Shelly gen2 energy meter can be used as a grid meter, or to masquarade for a
PV-inverter, as is already the case for our other energy meters. It can also be
used as an AC-meter. Piggybacking a PV-inverter on L2 is **NOT** supported
(yet, maybe).

## HowTo Install:
First! You must use the beta release of VenusOS (Currently v3.00~42)
Code only works for the new Shelly Pro EM3 meter

ssh to VenusOS (for example ssh root@192.168.XX.XX)
then run:
/opt/victronenergy/swupdate-scripts/set-feed.sh candidate
opkg update && opkg install git
git -C /data/ clone --recurse-submodules https://github.com/victronenergy/dbus-shelly.git && cd /data/dbus-shelly
python3 dbus_shelly.py

To update:
cd /data/dbus-shelly
git checkout master
git pull
git submodule update --init
