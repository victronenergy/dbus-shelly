# Venus driver for Shelly energy meters

This is a VenusOS driver for Gen2+ Shelly switches and energy meters.

# Connection
The shelly and the GX should be in the same network. The GX device discovers the shelly over mDNS. Found shelly devices are listed under the service `com.victronenergy.shelly/Devices/`. Each channel can be enabled individually. After enabling a channel by setting the `/Enabled` path to 1, a dedicated service will be registered on dbus. The type of service depends on the capabilities of the shelly.

- Shelly smart plugs with energy metering capabilities will be registered as `com.victronenergy.acload`. This is essentially a grid meter service but cannot be set to a role other than 'acload'. Using a shelly smart plug as grid meter does not make sense. Controls for the switchable output are found under this service as well, compliant to the [SwitchableOutput API](https://github.com/victronenergy/venus/wiki/dbus#switch).
- Shelly smart plugs without energy metering capabilities will be registered as `com.victronenergy.switch`.
- Shelly energy metering devices without a switchable output (so energy meters to be installed at an input or an output position) are registered as a standard grid meter. `com.victronenergy.<role>` with role equal to 'genset', 'pvinverter' or 'acload', depending on the setting and defaulting to 'acload'.

# Tested devices

The following shelly devices have been verified to work correctly:

- Shelly plus plug S
- Shelly Pro 4PM
- Shelly Pro 1PM
- Shelly Pro3EM (3 phase EM only)
- Shelly 1PM Gen4 (SW + EM)
- Shelly Mini 1PM Gen4 (SW + EM)
- Shelly Mini 1 Gen4 (SW only)
- Shelly Dimmer Gen3

# Legacy driver
The legacy shelly driver in which the shelly connects to the GX is still supported. This will likely be removed in a future version.