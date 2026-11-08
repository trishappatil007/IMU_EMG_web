"""Exercise the proposed device protocol without any physical hardware."""
import argparse
import asyncio
import json
import math
import time

import websockets


async def run(url):
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({'type':'hello', 'device_id':'simulated-pi',
                                  'sensor_model':'simulated_lsm6dso32', 'sample_rate_hz':50,
                                  'acceleration_unit':'m/s^2', 'angular_velocity_unit':'rad/s',
                                  'timestamp_clock':'simulator host Unix time'}))
        print('Backend:', await ws.recv())

        async def commands():
            async for raw in ws:
                print('Backend command:', raw)

        task = asyncio.create_task(commands())
        try:
            sequence = 0
            started = time.monotonic()
            while True:
                t = time.monotonic() - started
                await ws.send(json.dumps({'type':'sample', 'timestamp_ns':time.time_ns(),
                                          'sequence_number':sequence, 'ax':math.sin(t),
                                          'ay':math.cos(t), 'az':9.81,
                                          'gx':.1 * math.sin(t), 'gy':.1 * math.cos(t), 'gz':0}))
                if sequence % 50 == 0:
                    await ws.send(json.dumps({'type':'prediction','gesture':'idle','confidence':0.75}))
                sequence += 1
                await asyncio.sleep(.02)
        finally:
            task.cancel()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='ws://127.0.0.1:8000/ws/device', help='Backend device WebSocket URL')
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url))
    except KeyboardInterrupt:
        pass
