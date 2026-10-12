#!/usr/bin/env python3
"""
Test-only launcher: runs dbus_shelly.py with all timeouts and retry intervals
scaled by SHELLY_TEST_TIME_SCALE (e.g. 0.1), so timeout scenarios don't take
minutes. Nothing in the driver source is changed: the modules are imported
first, patched in memory, and then dbus_shelly.py is run as __main__.

What is scaled:
  - every RPC timeout, by wrapping aioshelly's RpcDevice.call_rpc_multiple
    (covers the driver's ping (2s) and RPC (4s) timeouts, and aioshelly's
    init calls)
  - aioshelly's connect timeout (DEVICE_INIT_TIMEOUT) and websocket heartbeat
  - the reconnect backoff and the connection manager heartbeat (discovery.py)

Retry counts (CONNECTION_RETRIES, PING_RETRIES) and the 1s sleep between pings
are left as they are.
"""
import os
import re
import runpy
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(REPO, "dbus_shelly.py")
SCALE = float(os.environ.get("SHELLY_TEST_TIME_SCALE", "1"))

# shelly_device does "from __main__ import VERSION, __file__ as processName", so provide
# both before importing it.
with open(DRIVER) as f:
	VERSION = re.search(r"^VERSION = '([^']+)'", f.read(), re.M).group(1)
__file__ = DRIVER

sys.path.insert(0, REPO)
sys.argv[0] = DRIVER

import discovery
from aioshelly.rpc_device import device as rpc_device_module, wsrpc

if SCALE != 1:
	rpc_device_module.DEVICE_INIT_TIMEOUT *= SCALE
	wsrpc.WS_HEARTBEAT *= SCALE

	_call_rpc_multiple = rpc_device_module.RpcDevice.call_rpc_multiple

	async def call_rpc_multiple(self, calls, timeout=rpc_device_module.DEVICE_IO_TIMEOUT):
		return await _call_rpc_multiple(self, calls, timeout * SCALE)

	rpc_device_module.RpcDevice.call_rpc_multiple = call_rpc_multiple

	discovery.BACKOFF_INITIAL_SECONDS *= SCALE
	discovery.RECONNECT_LOOP_HEARTBEAT_SECONDS *= SCALE

runpy.run_path(DRIVER, run_name="__main__")
