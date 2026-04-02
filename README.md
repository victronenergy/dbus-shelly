# Venus driver for Shelly energy meters

This is a VenusOS driver for Gen2+ Shelly switches and energy meters.

# Connection
The shelly and the GX should be in the same network. The GX device discovers the shelly over mDNS. Found shelly devices are listed under the service `com.victronenergy.shelly/Devices/`. Each channel can be enabled individually. After enabling a channel by setting the `/Enabled` path to 1, a dedicated service will be registered on dbus. The type of service depends on the capabilities of the shelly.

- Shelly smart plugs with energy metering capabilities will be registered as `com.victronenergy.<role>`. Allowed roles for this type of device are: 'acload', 'pvinverter' and 'heatpump', defaulting to 'acload'. Controls for the switchable output are found under this service as well, compliant to the [SwitchableOutput API](https://github.com/victronenergy/venus/wiki/dbus#switch).
- Shelly smart plugs without energy metering capabilities will be registered as `com.victronenergy.switch`.
- Shelly energy metering devices without a switchable output (so energy meters to be installed at an input or an output position) are registered as a standard grid meter. `com.victronenergy.<role>` with role equal to 'genset', 'pvinverter', 'acload' or 'heatpump', depending on the setting and defaulting to 'acload'.

# Supported RPC components
Shelly devices use Remote Procedure Calls (RPC) to send commands to devices and receive notifications and replies from the devices. More info [here](https://shelly-api-docs.shelly.cloud/gen2/General/RPCProtocol/). An [RPC component](https://shelly-api-docs.shelly.cloud/gen2/General/ComponentConcept) is an encapsulated functional unit which exposes methods used to control the device.

The RPC components a shelly device supports depend on the device type. For example, the [Shelly plus plug S](https://shelly-api-docs.shelly.cloud/gen2/Devices/Gen3/ShellyPlugSG3/) supports one instance of the [Switch](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Switch) component.

Since V2.00, dbus-shelly implements handlers for RPC components, which enables component-based support for devices.

## How to check if my device is supported
1. Find your device in the [Gen 2+ Device API list](https://shelly-api-docs.shelly.cloud/gen2/) (left pane - Devices)
2. Check if the device supports at least one of RPC components listed in the table below:

### RPC component to service type mapping
| RPC component      | Service type                     | Remarks                                                                                    |
|--------------------|----------------------------------|--------------------------------------------------------------------------------------------|
| EM and/or EMData   | *.acload, *.pvinverter, *.genset | Three-phase EM                                                                             |
| EM1 and/or EM1Data | *.acload, *.pvinverter, *.genset | Single-phase EM                                                                            |
| PM1                | *.acload, *.pvinverter, *.genset | Single-phase EM                                                                            |
| Switch             | *.switch, *.acload               | acload service when switch component reports voltage/current etc. switch service otherwise |
| Light              | *.switch                         | Switch type: Dimmable                                                                      |
| RGB                | *.switch                         | Switch type: RGB                                                                           |
| RGBW               | *.switch                         | Switch type: RGBW                                                                          |

If a device exposes x instances of an RPC component listed above, then x channels of that type will show up in the integration menu.

# Tested devices

The following shelly devices have been verified to work correctly:

- Shelly plus plug S
- Shelly Pro 4PM
- Shelly Pro 1PM
- Shelly Pro3EM (3 phase EM or 3x single phase EM)
- Shelly 1PM Gen4 (SW + EM)
- Shelly Mini 1PM Gen4 (SW + EM)
- Shelly Mini 1 Gen4 (SW only)
- Shelly Dimmer Gen3
- Shelly Plus RGBW PM (all profiles)
- Shelly Pro EM50 (2X em, 1x SW)
- Shelly Powerstrip gen4 (4x SW)
- Shelly PM mini gen3 (1x PM)
- Shelly Pro 1

# Shelly settings
This driver will only control runtime values like the on/off state and brightness. The settings of the shelly device will not be touched. There are some settings that may affect the behavior of your shelly device when controlled through the GX device:

- Night time settings (dimming devices only): When this setting is enabled, the max brightness and/or color will be set to a default level when the light is enabled.
- Transition duration (dimming devices only): When set to a nonzero value, the desired brightness / color change is not instantaneous.

# Legacy driver
The legacy shelly driver in which the shelly connects to the GX is still supported. This will likely be removed in a future version.