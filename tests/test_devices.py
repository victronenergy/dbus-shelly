"""
One mock per supported device type: enable each channel and check the D-Bus
paths and control in both directions.

All mocks are listed in /Settings/Shelly/IpAddresses before the driver starts.
"""
import pytest
from pytest import approx

from shelly_stack import make_stack, service_name, SHELLY_SERVICE, PRODUCT_ID_EM, PRODUCT_ID_SWITCH, PRODUCT_ID_INPUT

VOLTAGE = 230.0
APOWER = 1000.0
ENERGY_KWH = 12.345	# mock reports 12345 Wh forward per counter
RET_ENERGY_KWH = 2.345	# and 2345 Wh returned

EM = "aabbccdd1001"
EM1 = "aabbccdd1002"
PM1 = "aabbccdd1003"
DIMMER = "aabbccdd1004"
RGB = "aabbccdd1005"
CCT = "aabbccdd1006"
FLOOD = "aabbccdd1007"
RGBCCT = "aabbccdd1008"
NO_RET = "aabbccdd1009"
NO_RET_ZERO = "aabbccdd100a"

DEVICES = {
	# serial: (mock args, expected channel types)
	EM: (["--em"], ["em"]),
	EM1: (["--em1-channels", "2"], ["em1", "em1"]),
	PM1: (["--pm1-channels", "1"], ["em1"]),
	DIMMER: (["--light-channels", "1"], ["switch"]),
	RGB: (["--rgb"], ["switch"]),
	CCT: (["--cct"], ["switch"]),
	FLOOD: (["--flood"], ["input"]),
	RGBCCT: (["--rgbcct-channels", "1"], ["switch"]),
	# Like the Shelly Ogemray smart relay 25A: no returned energy counter at all.
	NO_RET: (["--em1-channels", "1", "--no-ret-energy"], ["em1"]),
	# Same, with a forward counter that is exactly 0.
	NO_RET_ZERO: (["--em1-channels", "1", "--no-ret-energy", "--energy-wh", "0"], ["em1"]),
}


def setup(s):
	for serial, (args, _) in DEVICES.items():
		s.add_mock(serial, serial, "--switch-channels", "0", "--apower", str(APOWER), "--voltage", str(VOLTAGE), *args)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "devices", setup, preseed_ips=True)


def enable(stack, serial, index=1):
	stack.wait_reachable(serial)
	stack.set_enabled(serial, index, 1)


def light_controls(stack, service):
	return list(stack.client.get_value(service, "/SwitchableOutput/0/LightControls"))


@pytest.mark.parametrize("serial", DEVICES)
def test_discovered(stack, serial):
	stack.wait_reachable(serial)
	assert stack.device(serial, "Supported") == 1
	for i, ch_type in enumerate(DEVICES[serial][1], start=1):
		assert stack.device(serial, f"{i}/Type") == ch_type


def test_em_3phase(stack):
	enable(stack, EM)
	name = service_name("acload", EM)
	items = stack.wait_service(name)
	assert items["/ProductId"] == PRODUCT_ID_EM
	assert items["/AllowedRoles"] == ["acload", "pvinverter", "genset", "heatpump"]
	assert "/PhaseSetting" not in items
	for l in (1, 2, 3):
		assert items[f"/Ac/L{l}/Voltage"] == VOLTAGE
		assert items[f"/Ac/L{l}/Power"] == APOWER
		assert items[f"/Ac/L{l}/Current"] == approx(APOWER / VOLTAGE)
		assert items[f"/Ac/L{l}/Energy/Forward"] == approx(ENERGY_KWH)
		assert items[f"/Ac/L{l}/Energy/Reverse"] == approx(RET_ENERGY_KWH)
	assert items["/Ac/Power"] == 3 * APOWER
	assert items["/Ac/Energy/Forward"] == approx(3 * ENERGY_KWH)
	assert items["/Ac/Energy/Reverse"] == approx(3 * RET_ENERGY_KWH)


def test_em_pvinverter_energy_swapped(stack):
	# A PV inverter's production is the energy the Shelly measures as returned, but on the GX
	# it belongs in the forward counter. So for the pvinverter role the counters are swapped.
	acload = service_name("acload", EM)
	pvinverter = service_name("pvinverter", EM)
	assert stack.client.set_value(acload, "/Role", "s", "pvinverter") == 0
	stack.wait_service_gone(acload)
	items = stack.wait_service(pvinverter)
	stack.wait_value(pvinverter, "/Ac/Energy/Forward", approx(3 * RET_ENERGY_KWH))
	items = stack.client.get_items(pvinverter)
	for l in (1, 2, 3):
		assert items[f"/Ac/L{l}/Energy/Forward"] == approx(RET_ENERGY_KWH)
		assert items[f"/Ac/L{l}/Energy/Reverse"] == approx(ENERGY_KWH)
	assert items["/Ac/Energy/Forward"] == approx(3 * RET_ENERGY_KWH)
	assert items["/Ac/Energy/Reverse"] == approx(3 * ENERGY_KWH)

	# Back to acload: no longer swapped
	assert stack.client.set_value(pvinverter, "/Role", "s", "acload") == 0
	stack.wait_service_gone(pvinverter)
	stack.wait_service(acload)
	stack.wait_value(acload, "/Ac/Energy/Forward", approx(3 * ENERGY_KWH))
	assert stack.client.get_value(acload, "/Ac/Energy/Reverse") == approx(3 * RET_ENERGY_KWH)


def test_em1_no_ret_energy(stack):
	# A device without a returned energy counter counts energy in both directions on the forward counter.
	enable(stack, NO_RET)
	acload = service_name("acload", NO_RET)
	stack.wait_service(acload)
	stack.wait_value(acload, "/Ac/Energy/Forward", approx(ENERGY_KWH))
	assert stack.client.get_value(acload, "/Ac/Energy/Reverse") == 0
	assert stack.client.get_value(acload, "/Ac/L1/Energy/Forward") == approx(ENERGY_KWH)

	# As a PV inverter, the forward counter is used as the produced energy.
	pvinverter = service_name("pvinverter", NO_RET)
	assert stack.client.set_value(acload, "/Role", "s", "pvinverter") == 0
	stack.wait_service_gone(acload)
	stack.wait_service(pvinverter)
	stack.wait_value(pvinverter, "/Ac/Energy/Forward", approx(ENERGY_KWH))
	items = stack.client.get_items(pvinverter)
	assert items["/Ac/Energy/Reverse"] == 0
	assert items["/Ac/L1/Energy/Forward"] == approx(ENERGY_KWH)
	assert items["/Ac/L1/Energy/Reverse"] == 0


def test_em1_no_ret_energy_zero(stack):
	# A forward counter that is exactly 0 is not published, also when it is used for a PV inverter.
	enable(stack, NO_RET_ZERO)
	acload = service_name("acload", NO_RET_ZERO)
	pvinverter = service_name("pvinverter", NO_RET_ZERO)
	stack.wait_service(acload)
	stack.wait_value(acload, "/Ac/Power", APOWER)
	assert stack.client.get_value(acload, "/Ac/Energy/Forward") is None
	assert stack.client.set_value(acload, "/Role", "s", "pvinverter") == 0
	stack.wait_service_gone(acload)
	stack.wait_service(pvinverter)
	stack.wait_value(pvinverter, "/Ac/Power", APOWER)
	stack.hold(lambda: stack.client.get_value(pvinverter, "/Ac/Energy/Forward") is None, "/Ac/Energy/Forward to stay unset", 1)
	assert stack.client.get_value(pvinverter, "/Ac/Energy/Reverse") is None


def test_em1_two_channels(stack):
	enable(stack, EM1, 1)
	enable(stack, EM1, 2)
	for ch in (0, 1):
		items = stack.wait_service(service_name("acload", EM1, ch))
		assert items["/ProductId"] == PRODUCT_ID_EM
		assert items["/PhaseSetting"] == 1
		assert items["/Ac/L1/Voltage"] == VOLTAGE
		assert items["/Ac/L1/Power"] == APOWER
		assert items["/Ac/Power"] == APOWER
		assert items["/Ac/Energy/Forward"] == approx(ENERGY_KWH)
		assert items["/Ac/L2/Power"] is None


def test_em1_phase_setting(stack):
	name = service_name("acload", EM1, 1)
	assert stack.client.set_value(name, "/PhaseSetting", "i", 2) == 0
	stack.wait_value(name, "/Ac/L2/Power", APOWER)
	assert stack.client.get_value(name, "/Ac/L1/Power") is None
	assert stack.setting(f"/Settings/Devices/shelly_{EM1}_1/1/PhaseSetting") == 2
	# Out of range is ignored. The onchange is async, so SetValue returns 0 regardless.
	stack.client.set_value(name, "/PhaseSetting", "i", 4)
	stack.hold(lambda: stack.client.get_value(name, "/PhaseSetting") == 2, "/PhaseSetting to stay 2", 1)
	assert stack.setting(f"/Settings/Devices/shelly_{EM1}_1/1/PhaseSetting") == 2


def test_pm1(stack):
	# Enabled after EM1, which is handled by the same handler class.
	enable(stack, PM1)
	items = stack.wait_service(service_name("acload", PM1))
	assert items["/Ac/L1/Power"] == APOWER
	assert items["/Ac/Power"] == APOWER
	assert items["/Ac/Energy/Forward"] == approx(ENERGY_KWH)


def test_em1_and_pm1_keep_their_components_in_either_order(stack):
	# EM1/EM1Data/PM1 share one handler class. Its _rpc_device_type list used to be a class
	# attribute that the first device mutated, so whichever of these was enabled second lost
	# its component. Re-enable both in the opposite order to the one above and check that
	# each still reports, whichever went first.
	# /Devices/<serial>/<index>/Enabled is 1-based; the service channel id is 0-based.
	pm1_name = service_name("acload", PM1)
	em1_name = service_name("acload", EM1)
	for serial, name in ((PM1, pm1_name), (EM1, em1_name)):
		stack.set_enabled(serial, 1, 0)
		stack.wait_service_gone(name)
	for serial, name in ((PM1, pm1_name), (EM1, em1_name)):
		enable(stack, serial)
		assert stack.wait_service(name)["/Ac/L1/Power"] == APOWER


def test_dimmer(stack):
	enable(stack, DIMMER)
	# No energy counters on the mock dimmer, so it is a plain switch service.
	name = service_name("switch", DIMMER)
	items = stack.wait_service(name)
	assert items["/ProductId"] == PRODUCT_ID_SWITCH
	assert items["/SwitchableOutput/0/Settings/Type"] == 2	# Dimmable
	assert items["/SwitchableOutput/0/State"] == 0
	assert items["/SwitchableOutput/0/Dimming"] == 50

	mock = stack.mocks[DIMMER]
	assert stack.client.set_value(name, "/SwitchableOutput/0/Dimming", "i", 30) == 0
	stack.wait_for(lambda: mock.rpc("Light.GetStatus", id=0) == {"id": 0, "output": True, "brightness": 30},
		"mock dimmed to 30% and on")
	stack.wait_value(name, "/SwitchableOutput/0/State", 1)

	mock.rpc("Light.Set", id=0, brightness=70)
	stack.wait_value(name, "/SwitchableOutput/0/Dimming", 70)

	assert stack.client.set_value(name, "/SwitchableOutput/0/State", "i", 0) == 0
	stack.wait_for(lambda: mock.rpc("Light.GetStatus", id=0)["output"] is False, "mock off")


@pytest.mark.xfail(strict=True, reason="shelly_handlers.py:789 handler_ainit -> _set_channel_type -> "
	"on_channel_type_changed(RGB) sends RGB.Set with the still empty LightControls, blanking the light")
def test_rgb_enable_keeps_device_state(stack):
	enable(stack, RGB)
	stack.wait_service(service_name("switch", RGB))
	# Mock starts at red, 50% brightness
	stack.hold(lambda: (st := stack.mocks[RGB].rpc("RGB.GetStatus", id=0))["rgb"] == [255, 0, 0] and st["brightness"] == 50,
		"device color and brightness unchanged", 1)


def test_rgb(stack):
	enable(stack, RGB)
	name = service_name("switch", RGB)
	items = stack.wait_service(name)
	assert items["/SwitchableOutput/0/Settings/Type"] == 11	# RGB

	mock = stack.mocks[RGB]
	assert stack.client.set_value(name, "/SwitchableOutput/0/LightControls", "ai", [120, 100, 80, 0, 0]) == 0
	stack.wait_for(lambda: (st := mock.rpc("RGB.GetStatus", id=0))["rgb"] == [0, 255, 0] and st["brightness"] == 80,
		"mock green at 80%")

	mock.rpc("RGB.Set", id=0, rgb=[0, 0, 255])
	stack.wait_for(lambda: light_controls(stack, name)[0] == 240, "LightControls hue to follow the device")


def test_cct_initial_light_controls(stack):
	enable(stack, CCT)
	name = service_name("switch", CCT)
	stack.wait_service(name)
	# Mock starts at 3000K, 50% brightness
	stack.wait_for(lambda: light_controls(stack, name)[2] == 50 and light_controls(stack, name)[4] == 3000,
		"initial LightControls", timeout=5)


def test_cct(stack):
	enable(stack, CCT)
	name = service_name("switch", CCT)
	items = stack.wait_service(name)
	assert items["/SwitchableOutput/0/Settings/Type"] == 12	# CCT

	mock = stack.mocks[CCT]
	assert stack.client.set_value(name, "/SwitchableOutput/0/LightControls", "ai", [0, 0, 60, 0, 4000]) == 0
	stack.wait_for(lambda: (st := mock.rpc("CCT.GetStatus", id=0))["ct"] == 4000 and st["brightness"] == 60,
		"mock at 4000K, 60%")

	mock.rpc("CCT.Set", id=0, ct=2700)
	stack.wait_for(lambda: light_controls(stack, name)[4] == 2700, "LightControls ct to follow the device")


def test_rgbcct(stack):
	enable(stack, RGBCCT)
	name = service_name("switch", RGBCCT)
	items = stack.wait_service(name)
	# Mock starts in rgb mode
	assert items["/SwitchableOutput/0/Settings/Type"] == 11	# RGB
	assert items["/SwitchableOutput/0/Settings/ValidTypes"] == (1 << 11) | (1 << 12)
	# Mock starts at white, 50% brightness, 4000K
	stack.wait_for(lambda: light_controls(stack, name)[2] == 50 and light_controls(stack, name)[4] == 4000,
		"initial LightControls", timeout=5)

	mock = stack.mocks[RGBCCT]
	assert stack.client.set_value(name, "/SwitchableOutput/0/LightControls", "ai", [120, 100, 80, 0, 0]) == 0
	stack.wait_for(lambda: (st := mock.rpc("RGBCCT.GetStatus", id=0))["mode"] == "rgb" and st["rgb"] == [0, 255, 0]
		and st["brightness"] == 80, "mock green at 80%")

	mock.rpc("RGBCCT.Set", id=0, brightness=80, rgb=[0, 0, 255])
	stack.wait_for(lambda: light_controls(stack, name)[0] == 240, "LightControls hue to follow the device")

	# Switching the type on D-Bus switches the mode on the device
	assert stack.client.set_value(name, "/SwitchableOutput/0/Settings/Type", "i", 12) == 0
	stack.wait_for(lambda: mock.rpc("RGBCCT.GetStatus", id=0)["mode"] == "cct", "mock in cct mode")
	assert stack.setting(f"/Settings/Devices/shelly_{RGBCCT}_0/0/Type") == 12

	assert stack.client.set_value(name, "/SwitchableOutput/0/LightControls", "ai", [0, 0, 60, 0, 2700]) == 0
	stack.wait_for(lambda: (st := mock.rpc("RGBCCT.GetStatus", id=0))["mode"] == "cct" and st["ct"] == 2700
		and st["brightness"] == 60, "mock at 2700K, 60%")

	mock.rpc("RGBCCT.Set", id=0, brightness=60, ct=5000)
	stack.wait_for(lambda: light_controls(stack, name)[4] == 5000, "LightControls ct to follow the device")

	# The mode of the device is leading: a mode change from outside updates the type and its setting
	mock.rpc("RGBCCT.Set", id=0, on=True, mode="rgb")
	stack.wait_value(name, "/SwitchableOutput/0/Settings/Type", 11)
	stack.wait_for(lambda: stack.setting(f"/Settings/Devices/shelly_{RGBCCT}_0/0/Type") == 11, "Type setting to follow the device")


def test_flood(stack):
	# Enabled by default, like Smoke
	name = service_name("digitalinput", FLOOD)
	items = stack.wait_service(name)
	assert items["/ProductId"] == PRODUCT_ID_INPUT
	assert items["/Type"] == 4	# Bilge alarm
	assert items["/Alarm"] == 0
	assert stack.enabled_setting(FLOOD, 1) == 1
	# Flood has no remote mute
	assert stack.client.set_value(name, "/Mute", "i", 1) != 0

	mock = stack.mocks[FLOOD]
	mock.rpc("Mock.SetAlarm", alarm=True)
	stack.wait_value(name, "/Alarm", 2)
	assert stack.client.get_value(name, "/State") == 9

	# With the alarm setting off, the state still shows the alarm but /Alarm does not.
	assert stack.client.set_value(name, "/Settings/AlarmSetting", "i", 0) == 0
	stack.wait_value(name, "/Alarm", 0)
	assert stack.client.get_value(name, "/State") == 9
	assert stack.client.set_value(name, "/Settings/AlarmSetting", "i", 1) == 0

	mock.rpc("Mock.SetAlarm", alarm=False)
	stack.wait_value(name, "/State", 8)
	assert stack.client.get_value(name, "/Alarm") == 0


def test_flood_product_name(stack):
	items = stack.wait_service(service_name("digitalinput", FLOOD))
	assert items["/ProductName"] == "Shelly input device"
