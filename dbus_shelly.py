#!/usr/bin/python3

VERSION = "0.6"

import sys
import os
import asyncio
import websockets
import logging
import ssl
import json
import itertools
from argparse import ArgumentParser

import asyncio

# 3rd party
from dbus_next.constants import BusType

# aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import Service

# local modules
from meter import Meter

wslogger = logging.getLogger('websockets.server')
wslogger.setLevel(logging.INFO)
wslogger.addHandler(logging.StreamHandler())

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

tx_count = itertools.cycle(range(1000, 5000))

class Server(object):
	def __init__(self, make_meter):
		self.meters = {}
		self.make_meter = make_meter

	async def __call__(self, socket, path):
		# If we have a connection to the meter already, kill it and
		# make a new one
		if (m := self.meters.get(socket.remote_address)) is not None:
			m.destroy()
			del self.meters[socket.remote_address]

		self.meters[socket.remote_address] = m = self.make_meter()

		# Tell the meter to send a full status
		await socket.send(json.dumps({
			"id": "GetDeviceInfo-{}".format(next(tx_count)),
			"method":"Shelly.GetDeviceInfo"
		}))

		while not m.destroyed:
			# Decode data, and dispatch it to the gevent mainloop
			try:
				data = json.loads(await socket.recv())
			except ValueError:
				logger.error("Malformed data in json payload")
			except websockets.exceptions.WebSocketException:
				logger.info("Lost connection to " + str(socket.remote_address))
				m.destroy()
				break
			else:
				if str(data.get('id', '')).startswith('GetDeviceInfo-'):
					if not await m.start(*socket.remote_address, data):
						logger.info("Failed to start meter for " + str(socket.remote_address))
						m.destroy()
						break
				else:
					await m.update(data)

		del self.meters[socket.remote_address]

def main():
	parser = ArgumentParser(description=sys.argv[0])
	parser.add_argument('--dbus', help='dbus bus to use, defaults to system',
			default='system')
	parser.add_argument('--debug', help='Turn on debug logging',
			default=False, action='store_true')
	args = parser.parse_args()

	logging.basicConfig(format='%(levelname)-8s %(message)s',
			level=(logging.DEBUG if args.debug else logging.INFO))

	bus_type = {
		"system": BusType.SYSTEM,
		"session": BusType.SESSION
	}.get(args.dbus, BusType.SESSION)

	mainloop = asyncio.get_event_loop()
	mainloop.run_until_complete(
		websockets.serve(Server(lambda: Meter(bus_type)), '', 8000))

	try:
		logger.info("Starting main loop")
		mainloop.run_forever()
	except KeyboardInterrupt:
		mainloop.stop()


if __name__ == "__main__":
    main()
