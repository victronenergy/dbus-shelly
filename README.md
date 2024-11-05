# Venus driver for Shelly energy meters

This is a VenusOS driver for Gen2+ Shelly power meters (EM and PM series).

## Preferred mode of installation
Your GX device needs a static/fixed IP address. The energy meter will push
data to the GX device using the websockets protocol on port 8000. Set the 
"Server" in the "Outbound websocket settings" of your Shelly device to:
`ws://<IP address of your GX>:8000`

## Supported configurations
A Shelly gen2+ energy meter can be used as a grid meter, or to masquarade for a
PV-inverter, as is already the case for our other energy meters. It can also be
used as an AC-meter. Piggybacking a PV-inverter on L2 is **NOT** supported.
