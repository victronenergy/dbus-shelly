# Venus driver for Shelly energy meters

**NOTE** this project was abandoned before making its way into Venus OS. Therefore, see GX documentation, ie. docs of for example the Cerbo GX, for supported energy meters. And see https://www.victronenergy.com/meters-and-sensors/energy-meter.


This is a VenusOS driver for Gen2 Shelly energy meters.

## Preferred mode of installation
Your GX device needs a static/fixed IP address. The energy meter will push
data to the GX device using the websockets protocol.

## Supported configurations
A Shelly gen2 energy meter can be used as a grid meter, or to masquarade for a
PV-inverter, as is already the case for our other energy meters. It can also be
used as an AC-meter. Piggybacking a PV-inverter on L2 is **NOT** supported
(yet, maybe).
