# Venus driver for Shelly energy meters

This is a VenusOS driver for the Shelly-EM and Shelly-3EM energy meters.
It uses the HTTP interface of the meter, and supports mdns detection.

## Preferred mode of installation
Ideally your Shelly-EM/3EM should be linked with the WiFi AP of your GX
device to avoid an additional point of failure in your internet equipment,
but it will also work when connected to the same network as your GX device.

## MDNS detection
A [mdns request][1] for http services will be sent periodically, and any device
that has a name starting with `shellyem` or `shelly3em` will be turned into
an energy meter.

## Configure in localsettings
It is possible to configure additional locations in localsettings, at path
`/Settings/Shelly/Devices`. The configuration is a comma-seperated list of
IP:port values.

## Supported configurations
A Shelly EM/3EM can be used as a grid meter, or to masquarade for a
PV-inverter, as is already the case for our other energy meters. It can also be
used as an AC-meter. Piggybacking a PV-inverter on L2 is **NOT** supported.

[1]: https://shelly-api-docs.shelly.cloud/gen1/#mdns-discovery
