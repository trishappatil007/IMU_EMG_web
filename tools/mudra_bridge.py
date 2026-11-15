"""Experimental Mudra Link FFF4 BLE-to-WebSocket EMG bridge.

Run with --scan to find the band, then --address ADDRESS to start the bridge.
The 112-byte frame layout and BLE commands come from the supplied capture scripts;
they have not been validated with the hardware used by this app.
"""
import argparse
import asyncio
import json
import struct
import time
from contextlib import suppress

CMD = '0000fff1-0000-1000-8000-00805f9b34fb'
SNC = '0000fff4-0000-1000-8000-00805f9b34fb'
ENABLE_SNC = bytes([0x06, 0x00, 0x01])
DISABLE_SNC = bytes([0x06, 0x00, 0x00])
FRAME = struct.Struct('<54hI')  # 18 samples x 3 signed counts, then device u32 µs.


def decode_frame(payload: bytes):
    """Decode only the layout shown in the supplied 112-byte capture script."""
    if len(payload) != FRAME.size:
        raise ValueError(f'Unsupported FFF4 frame length {len(payload)} bytes; expected {FRAME.size}.')
    fields = FRAME.unpack(payload)
    values = fields[:-1]
    return [list(values[i:i + 3]) for i in range(0, len(values), 3)], fields[-1]


async def scan_devices():
    from bleak import BleakScanner
    print('Scanning for Bluetooth devices for 12 seconds...')
    devices = await BleakScanner.discover(timeout=12, return_adv=True)
    for device, adv in devices.values():
        name = device.name or adv.local_name or '(no name)'
        print(f'{device.address}  {name}  RSSI: {adv.rssi}')


async def run(address: str, url: str, sample_rate: float | None):
    from bleak import BleakClient, BleakScanner
    import websockets

    print('Looking for the band; make sure Bluetooth is on and the band is awake...')
    device = await BleakScanner.find_device_by_address(address, timeout=12)
    if device is None:
        raise RuntimeError('Band not found. Wake it, close other apps using it, and run --scan again.')
    frames = asyncio.Queue(maxsize=128)
    first_frame, failed, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reason = []
    next_frame = 0

    def on_frame(_characteristic, data):
        nonlocal next_frame
        if failed.is_set():
            return
        try:
            samples, device_us = decode_frame(bytes(data))
            packet = {'type': 'emg_frame', 'frame_sequence': next_frame,
                      'device_timestamp_us': device_us, 'host_received_ns': time.time_ns(),
                      'samples': samples}
            frames.put_nowait(packet)
            next_frame += 1
            first_frame.set()
        except (ValueError, asyncio.QueueFull) as exc:
            reason.append(str(exc) if isinstance(exc, ValueError) else 'BLE queue overflow; recording may be incomplete.')
            failed.set()

    def on_disconnect(_client):
        disconnected.set()

    async with websockets.connect(url, open_timeout=10) as ws:
        await ws.send(json.dumps({'type': 'hello', 'device_id': 'mudra-link',
                                  'sample_rate_hz': sample_rate}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if reply.get('type') != 'hello_ack':
            raise RuntimeError(reply.get('message', 'Backend rejected the Mudra bridge.'))
        async with BleakClient(device, pair=True, timeout=30, disconnected_callback=on_disconnect) as client:
            print('Band connected. Enabling its raw EMG notifications...')
            enabled = False
            try:
                await client.start_notify(SNC, on_frame)
                await asyncio.sleep(0.5)
                await client.write_gatt_char(CMD, ENABLE_SNC, response=True)
                enabled = True
                waits = [asyncio.create_task(first_frame.wait()), asyncio.create_task(failed.wait()),
                         asyncio.create_task(disconnected.wait())]
                try:
                    done, _ = await asyncio.wait(waits, timeout=8, return_when=asyncio.FIRST_COMPLETED)
                    if failed.is_set():
                        raise RuntimeError(reason[0])
                    if disconnected.is_set():
                        raise RuntimeError('The band disconnected.')
                    if not done or not first_frame.is_set():
                        raise RuntimeError('No EMG notifications received. Check pairing and use the official Mudra app to activate the band if needed.')
                finally:
                    for task in waits:
                        task.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)

                print('Receiving EMG. Select Mudra Link EMG in the browser. Press Ctrl+C to stop.')

                async def send_frames():
                    while True:
                        await ws.send(json.dumps(await frames.get(), separators=(',', ':')))

                async def read_backend():
                    async for message in ws:
                        value = json.loads(message)
                        if value.get('type') == 'error':
                            raise RuntimeError('Backend rejected the stream: ' + value.get('message', 'unknown error'))
                    raise RuntimeError('Backend disconnected.')

                async def watch_failures():
                    tasks = [asyncio.create_task(failed.wait()), asyncio.create_task(disconnected.wait())]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        raise RuntimeError(reason[0] if failed.is_set() else 'The band disconnected.')
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

                tasks = [asyncio.create_task(send_frames()), asyncio.create_task(read_backend()),
                         asyncio.create_task(watch_failures())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                if enabled and client.is_connected:
                    with suppress(Exception):
                        await client.write_gatt_char(CMD, DISABLE_SNC, response=True)
                if client.is_connected:
                    with suppress(Exception):
                        await client.stop_notify(SNC)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scan', action='store_true', help='List nearby Bluetooth devices and exit.')
    parser.add_argument('--address', help='Band address reported by --scan.')
    parser.add_argument('--url', default='ws://127.0.0.1:8000/ws/mudra', help='Backend Mudra WebSocket URL.')
    parser.add_argument('--sample-rate', type=float, default=None,
                        help='Optional measured/known nominal EMG Hz; leave unset when unknown.')
    args = parser.parse_args()
    if not args.scan and not args.address:
        parser.error('Use --scan to discover the band, or provide --address.')
    if args.sample_rate is not None and not 0 < args.sample_rate <= 5000:
        parser.error('--sample-rate must be greater than zero and at most 5000 Hz.')
    try:
        asyncio.run(scan_devices() if args.scan else run(args.address, args.url, args.sample_rate))
    except ImportError as exc:
        print(f'Missing dependency {exc.name}. Install requirements-mudra.txt in the project environment.')
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f'Mudra bridge stopped: {type(exc).__name__}: {exc}')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
