# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this 
repository.

## AI rules
The following rules override all other preferences.

- Never suggest working directly on the `master` branch.
- Do not refactor code unless explicitly asked
- Do not reformat code for style
- Do not replace existing patterns with “modern” ones
- Do not guess hardware behavior
- Do not push commits
- Only suggest git commits, do not commit
- Suggested git commits must not include "Fixes ..." or "Closes ..." when referencing a git issue. Instead, reference the issue without closing it.
- Never add AI attribution text to git commits (no "Generated with Claude Code", "Co-Authored-By: Claude", or similar).

When in doubt:
- Ask a question
- Or provide analysis without code changes

## What this is

A Venus OS (Victron GX) driver that exposes Gen2+ Shelly devices on D-Bus. It discovers Shellys (mDNS and manually entered IPs), lists them under `com.victronenergy.shelly/Devices/`, and for each channel the user enables it registers a dedicated service (`acload`/`pvinverter`/`genset`/`heatpump`, `switch`, or `digitalinput`). The README holds the RPC-component → service-type mapping and the list of tested devices. Update both when you add support for a device or component.

## Commands

There is no test suite. CI (`.github/workflows/main.yml`) only runs:

```sh
git submodule update --init          # ext/aiovelib is a required submodule
make testinstall                     # installs to a temp dir and runs `dbus_shelly.py --help`
```

Runtime deps (see CI): `dbus-python dbus_fast aioshelly websockets zeroconf async_timeout s2_python==0.8.1` (plus `aiohttp` for the mock server). A `.venv` exists locally.

Running locally (needs a D-Bus with `com.victronenergy.settings` available, or the settings wait times out):

```sh
python3 dbus_shelly.py --dbus session --debug
python3 dbus_shelly.py --dbus session --mock            # also spawns mock devices from mock/mock_devices.json
python3 dbus_shelly.py --mock path/to/devices.json
```

Mock devices (`mock/mock_shelly_server.py`, one aiohttp process per device, ports 8022+) emulate the Shelly RPC/websocket API, including Switch+EM, Smoke and Flood. They are not announced over mDNS, so add them through `/IpAddresses` on the `com.victronenergy.shelly` service as a comma-separated list of `ip[:port]`, e.g. `127.0.0.1:8022,127.0.0.1:8023`.

When you add a new source file, add it to `FILES` in the `Makefile`. Otherwise it won't be installed. The same goes for new aiovelib modules in `LIB`.

## Code style

Tabs for indentation. Python 3.12 (CI), asyncio throughout.

## Architecture

There are two independent paths, both started from `dbus_shelly.py`:

- **Legacy path** (`meter.py`): a websocket server on port 8000 that Shellys connect *to*. It is kept for backwards compatibility and is slated for removal.
- **Current path** (`discovery.py` → `shelly_device.py` → `shelly_handlers.py`): the GX connects to the Shellys using `aioshelly`.

### Discovery layer (`discovery.py`)

- `ShellyDiscovery` is the composition root. It owns the `com.victronenergy.shelly` service, the `/Settings/Shelly/IpAddresses` setting, and `/Refresh`.
- `MdnsDiscovery` and `ManualIpDiscovery` are endpoint sources. Both feed `ShellyDeviceCache` through `add_endpoint`/`remove_endpoint`. An endpoint is removed only when its source matches.
- `ShellyDeviceCache` keys endpoints by host or serial and holds a `ConnectMeta` retry policy for each one. Manual endpoints retry forever. mDNS endpoints retry within a 24h window with exponential backoff (10s → 10min).
- `ShellyConnectionManager` is a background loop. It probes endpoints that are due for a retry and pushes status changes (`ProbeStatus`) back into the cache.
- `ShellyManager` consumes the cache's change queue. It maintains the `/Devices/<serial>/...` D-Bus inventory (including `/Reachable` and the per-channel `/Enabled`) and owns the `ShellyDevice` instances. Channel enable/disable requests are serialized through `_channel_op_queue`. Failed channel starts have their own retry tasks.

### Device layer (`shelly_device.py`)

- `ShellyDevice` wraps one `aioshelly` `RpcDevice`. It gets capabilities from `Shelly.GetComponents` and builds `channel_info` from them. It also handles reconnection: it pings a few times before a full restart and emits `ShellyEvent`s on a queue. `_device_lock` guards lifecycle operations. Channel init is awaited *outside* that lock so that a reconnect can call `start()` in the meantime.
- Channel indices in `channel_info` are assigned in the order of `FUNCTIONAL_HANDLERS`, and settings are indexed by them. Add new kinds only at the end of that list.
- `ShellyChannel` owns one D-Bus `Service` plus the localsettings connection. It is named `com.victronenergy.<type>.shelly_<serial>_<channel>`, and its settings live under `/Settings/Devices/shelly_<serial>_<channel>/`.
- Sleepy (battery) devices are those with a `digitalinput` channel. While they are asleep, their service stays registered with `/Connected=0`, and they are not reconnect-looped aggressively.

### Handler layer (`shelly_handlers.py`)

Each RPC component maps to a handler class through a decorator:

```python
@register_handler('Switch', kind=HANDLER_KIND_SWITCH)
class ShellyHandler_switch(ShellyHandler_switch_base): ...
```

- `kind` decides which channel type the handler serves: `switch`, `em1`, `em`, `digitalinput`, or `generic`. Generic handlers such as `Temperature`, `DevicePower` and `Sys` attach to any channel, but they don't make a device "supported" on their own.
- `aggregate=True` collapses every id of that component into a single channel. `Voltmeter` does this.
- A handler registered for several components (e.g. `EM1` + `EM1Data`) is instantiated once per channel. `_check_rpc_type_supported` removes the components the device doesn't actually expose.
- Lifecycle: `ShellyHandler.create` → `ainit()`, which checks `GetStatus`, calls `handler_ainit()` to add D-Bus paths and settings, then calls `force_update()`. After that, `update(status_json, cap)` receives status notifications. `refresh()` runs after a short reconnect, and the `restart` callback does a full channel restart.
- Composition is done with mixins: `Shelly_EM_base` (EM paths and role selection), `ShellyHandler_channel_config_mixin` (custom names and similar), `ThrottledUpdaterMixin` (for dimmers and RGB), and `ShellyHandler_digitalinput_alarm_base` (Smoke and Flood).
- Switch paths follow the Venus `SwitchableOutput` API (`/SwitchableOutput/<id>/State|Status|Settings/...`). `OutputType` and `OutputFunction` live in `utils.py`.

### S2 / opportunity loads (`shelly_s2.py`)

`ShellyHandlerS2Mixin` adds S2 resource-manager support (the OMBC and NOCTRL control types) to switch handlers, which enables the "opportunity load" output function. It depends on `s2_python` and `aiovelib.s2`. If the import fails, `shelly_handlers.py` falls back gracefully and the feature is disabled.

### Gotchas

- `shelly_device.py` does `from __main__ import VERSION`, so modules must be run through `dbus_shelly.py`. Importing them from some other entrypoint breaks. `VERSION` lives in `dbus_shelly.py`.
- Capabilities are stored in lowercase (`register_handler` lowercases them). Compare them in lowercase.
- Product IDs: `0xB034` (EM) and `0xB075` (switch). Smoke and Flood share the placeholder `0xB0A0`.
