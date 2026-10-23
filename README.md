# Venus driver for Shelly energy meters

[![CI](https://github.com/victronenergy/dbus-shelly/actions/workflows/main.yml/badge.svg?branch=master)](https://github.com/victronenergy/dbus-shelly/actions/workflows/main.yml)

This is a VenusOS driver for Gen2+ Shelly switches and energy meters.

# Connection
The shelly and the GX should be in the same network. The GX device discovers the shelly over mDNS. Automatic scanning can be disabled by setting `com.victronenergy.shelly/AutoScan` to 0, in which case devices have to be added manually through `/IpAddresses`. Found shelly devices are listed under the service `com.victronenergy.shelly/Devices/`. Each channel can be enabled individually. After enabling a channel by setting the `/Enabled` path to 1, a dedicated service will be registered on dbus. The type of service depends on the capabilities of the shelly.

Switch and energy metering channels default to disabled and require an explicit opt-in, since enabling them can drive real loads. Smoke and Flood channels default to enabled on first discovery instead: they can't actuate anything, and enabling them right away -- while the device is confirmed awake from the discovery probe -- avoids having to catch a battery-powered sensor during one of its brief wake windows just to turn it on.

- Shelly smart plugs with energy metering capabilities will be registered as `com.victronenergy.<role>`. Allowed roles for this type of device are: 'acload', 'pvinverter' and 'heatpump', defaulting to 'acload'. Controls for the switchable output are found under this service as well, compliant to the [SwitchableOutput API](https://github.com/victronenergy/venus/wiki/dbus#switch).
- Shelly smart plugs without energy metering capabilities will be registered as `com.victronenergy.switch`.
- Shelly energy metering devices without a switchable output (so energy meters to be installed at an input or an output position) are registered as a standard grid meter. `com.victronenergy.<role>` with role equal to 'genset', 'pvinverter', 'acload' or 'heatpump', depending on the setting and defaulting to 'acload'.
- Shelly smoke detectors are registered as `com.victronenergy.digitalinput` with `/Type` fixed to 'Smoke alarm', tying into Venus OS's existing digital input alarm/notification handling. Battery level is reported via non-standard `/BatteryVoltage` and `/BatteryPercent` paths, following the convention used by the battery-powered sensors in [dbus-ble-sensors](https://github.com/victronenergy/dbus-ble-sensors). The alarm can be silenced remotely through a writeable `/Mute` path.
- Shelly flood/leak sensors are registered the same way, as `com.victronenergy.digitalinput`. There's no dedicated flood/water type in Venus's digitalinput enum, so `/Type` is fixed to 'Bilge alarm' -- the closest existing match for water-intrusion detection. Unlike Smoke, the Flood RPC component has no remote mute call: `/Mute` is read-only and only reflects the state set through the device's physical button.

  Note that battery-powered sensors (devices with a DevicePower component, such as Smoke and Flood) spend almost all their time in deep sleep (waking roughly once every 24h, plus on button press or an actual alarm). Their dbus service stays registered with its last known values (and `/Connected` set to 0) while asleep, and reconnects automatically once seen again over mDNS on its next wake-up. Keep automatic scanning (`/AutoScan`) enabled for these sensors: with scanning disabled, a sensor added through `/IpAddresses` is not reliably reconnected after it wakes up. In that case, trigger `/Refresh` while the sensor is awake (e.g. after pressing its button) to make the driver connect to it.

- Shelly temperature sensors are registered as `com.victronenergy.temperature`. This applies to a Temperature component with an id of its own, such as the probes of a Sensor Add-on (`temperature:100` and up). A Temperature component that shares its id with another channel (e.g. `temperature:0` next to `switch:0`) is part of that channel and only adds `/Temperature` to its service. `/Status` is 0 (Ok) while the sensor reports a valid reading and 4 (Unknown) when it reports an error. Temperature channels default to disabled, except on battery-powered devices.

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
| Voltmeter          | *.acload, *.pvinverter, *.genset, *heatpump | Single-phase EM with only voltage readings                                      |
| Switch             | *.switch, *.acload               | acload service when switch component reports voltage/current etc. switch service otherwise |
| Light              | *.switch                         | Switch type: Dimmable                                                                      |
| RGB                | *.switch                         | Switch type: RGB                                                                           |
| RGBW               | *.switch                         | Switch type: RGBW                                                                          |
| CCT                | *.switch                         | Switch type: CCT                                                                           |
| RGBCCT             | *.switch                         | Switch type: RGB or CCT                                                                    |
| CB                 | *.switch                         | Switch type: Toggle                                                                        |
| Smoke              | *.digitalinput                   | Smoke alarm. DevicePower (if present) adds battery info to the same service                |
| Flood              | *.digitalinput                   | Bilge alarm (no dedicated flood type in Venus). DevicePower (if present) adds battery info |
| Temperature        | *.temperature                    | Only when its id is not used by another channel (e.g. Sensor Add-on). Otherwise adds /Temperature to that channel's service. DevicePower (if present) adds battery info |

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
- Shelly Duo Bulb Gen3 (CCT)
- Shelly Pro 3CB (SW [CB] + EM [Voltmeter])
- Shelly Plus Smoke (Smoke + DevicePower)

# Shelly settings
This driver will only control runtime values like the on/off state and brightness. The settings of the shelly device will not be touched. There are some settings that may affect the behavior of your shelly device when controlled through the GX device:

- Night time settings (dimming devices only): When this setting is enabled, the max brightness and/or color will be set to a default level when the light is enabled.
- Transition duration (dimming devices only): When set to a nonzero value, the desired brightness / color change is not instantaneous.

# Legacy driver
The legacy shelly driver in which the shelly connects to the GX is still supported. This will likely be removed in a future version.