"""Protocol and recording tests; no physical Bluetooth hardware is required."""
import csv
import json
import struct
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.main import create_app
from tools.mudra_bridge import decode_frame, run


def until(ws, kind, predicate=lambda item: True):
    for _ in range(500):
        item = ws.receive_json()
        if item.get('type') == kind and predicate(item):
            return item
    raise AssertionError(f'No matching {kind} message')


def frame(sequence, timestamp=123456):
    return {'type': 'emg_frame', 'frame_sequence': sequence,
            'device_timestamp_us': timestamp, 'host_received_ns': 1700000000000000000 + sequence,
            'samples': [[i, -i, 100 + i] for i in range(18)]}


def start_command(destination):
    return {'type': 'start_recording', 'countdown_seconds': 0,
            'requested_sample_rate_hz': None, 'destination': destination,
            'metadata': {'participant_id': 'P1', 'session_id': 'S1',
                         'gesture': 'idle'}}


class MudraTests(unittest.TestCase):
    def test_decoder_preserves_signed_channels_and_timestamp(self):
        values = [value for i in range(18) for value in (i, -i, 100 + i)]
        samples, timestamp = decode_frame(struct.pack('<54hI', *values, 0xFFFFFFFE))
        self.assertEqual(samples, frame(0)['samples'])
        self.assertEqual(timestamp, 0xFFFFFFFE)
        with self.assertRaisesRegex(ValueError, 'Unsupported FFF4 frame length'):
            decode_frame(b'\x00' * 118)

    def test_emg_only_trial_review_and_local_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'approved').mkdir()
            with TestClient(create_app(root / 'staging')) as client, client.websocket_connect('/ws') as browser:
                until(browser, 'status')
                with client.websocket_connect('/ws/mudra') as device:
                    device.send_json({'type': 'hello', 'device_id': 'test-mudra', 'sample_rate_hz': None})
                    self.assertEqual(device.receive_json()['type'], 'hello_ack')
                    device.send_json(frame(0))
                    until(browser, 'status', lambda m: m['mudra_receiving'])
                    browser.send_json({'type': 'set_source', 'source': 'mudra'})
                    status = until(browser, 'status', lambda m: m['source'] == 'mudra')
                    self.assertIsNone(status['sample_rate_hz'])
                    browser.send_json(start_command({'kind': 'local', 'folder': str(root / 'approved')}))
                    until(browser, 'status', lambda m: m['phase'] == 'recording')
                    device.send_json(frame(1, 0xFFFFFFFE))
                    batch = until(browser, 'emg_batch', lambda m: m['source'] == 'mudra')
                    self.assertEqual(batch['samples'][0]['values'], [0, 0, 100])
                    browser.send_json({'type': 'stop_recording'})
                    review = until(browser, 'status', lambda m: m['phase'] == 'review')
                    self.assertEqual(review['emg_recorded_samples'], 18)
                    self.assertEqual(review['emg_quality']['status'], 'not assessed')
                    self.assertTrue(review['emg_quality']['data_integrity'])
                    browser.send_json({'type': 'review_trial', 'decision': 'accepted'})
                    self.assertIn('Enter a reason', until(browser, 'error')['message'])
                    browser.send_json({'type': 'review_trial', 'decision': 'accepted',
                                       'override_reason': 'Pilot review of unvalidated signal'})
                    saved = until(browser, 'status', lambda m: m['phase'] == 'idle')
                    self.assertEqual(len(saved['export_result']['files']), 2)
                    with (root / 'approved' / review['emg_file']).open(newline='') as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertEqual(len(rows), 18)
                    self.assertEqual([rows[2][f'emg_{i}'] for i in range(1, 4)], ['2', '-2', '102'])
                    self.assertEqual(rows[2]['sample_in_frame'], '2')
                    self.assertEqual(rows[2]['device_timestamp_us'], str(0xFFFFFFFE))
                    self.assertEqual(rows[2]['timestamp_ns'], str(1700000000000000001))
                    info = json.loads((root / 'approved' / review['metadata_file']).read_text())
                    self.assertEqual(info['review_status'], 'accepted')
                    self.assertEqual(info['emg_unit'], 'raw signed 16-bit counts')
                    self.assertIsNone(info['emg_sample_rate_hz'])
                    self.assertIn('cannot detect Bluetooth packet loss', info['emg_sequence_note'])

    def test_gap_invalidates_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(create_app(Path(tmp))) as client, client.websocket_connect('/ws') as browser:
                until(browser, 'status')
                with client.websocket_connect('/ws/mudra') as device:
                    device.send_json({'type': 'hello', 'device_id': 'test-mudra'})
                    device.receive_json()
                    device.send_json(frame(0))
                    until(browser, 'status', lambda m: m['mudra_receiving'])
                    browser.send_json({'type': 'set_source', 'source': 'mudra'})
                    until(browser, 'status', lambda m: m['source'] == 'mudra')
                    browser.send_json(start_command({'kind': 'local'}))
                    until(browser, 'status', lambda m: m['phase'] == 'recording')
                    device.send_json(frame(1))
                    until(browser, 'emg_batch', lambda m: m.get('recorded_samples') == 18)
                    device.send_json(frame(3))
                    result = until(browser, 'status', lambda m: m['phase'] == 'review' and m['error'] and 'stream error' in m['error'])
                    self.assertEqual(result['emg_recorded_samples'], 18)
                    info = json.loads((Path(tmp) / result['metadata_file']).read_text())
                    self.assertEqual(info['review_status'], 'invalid')
                    browser.send_json({'type': 'review_trial', 'decision': 'accepted', 'override_reason': 'try'})
                    self.assertIn('failed recording', until(browser, 'error')['message'])

    def test_disconnect_invalidates_active_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(create_app(Path(tmp))) as client, client.websocket_connect('/ws') as browser:
                until(browser, 'status')
                with client.websocket_connect('/ws/mudra') as device:
                    device.send_json({'type': 'hello', 'device_id': 'test-mudra'})
                    device.receive_json(); device.send_json(frame(0))
                    until(browser, 'status', lambda m: m['mudra_receiving'])
                    browser.send_json({'type': 'set_source', 'source': 'mudra'})
                    until(browser, 'status', lambda m: m['source'] == 'mudra')
                    browser.send_json(start_command({'kind': 'local'}))
                    until(browser, 'status', lambda m: m['phase'] == 'recording')
                    device.send_json(frame(1))
                    until(browser, 'emg_batch', lambda m: m.get('recorded_samples') == 18)
                review = until(browser, 'status', lambda m: m['phase'] == 'review' and not m['mudra_connected'])
                info = json.loads((Path(tmp) / review['metadata_file']).read_text())
                self.assertEqual(info['review_status'], 'invalid')
                self.assertTrue(info['recording_error'])


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ble_notification_is_forwarded_and_cleaned_up(self):
        import asyncio
        values = [value for i in range(18) for value in (i, -i, 100 + i)]
        raw = struct.pack('<54hI', *values, 98765)
        transmitted = []
        ready = asyncio.Event()
        writes = []

        class Scanner:
            @staticmethod
            async def find_device_by_address(address, timeout):
                self.assertEqual(address, 'test-device')
                return object()

        class Client:
            def __init__(self, device, pair, timeout, disconnected_callback):
                self.is_connected = True
                self.callback = None
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                self.is_connected = False
            async def start_notify(self, characteristic, callback):
                self.callback = callback
            async def stop_notify(self, characteristic):
                writes.append('stop_notify')
            async def write_gatt_char(self, characteristic, data, response):
                writes.append(bytes(data))
                if bytes(data) == bytes([6, 0, 1]):
                    self.callback(None, raw)

        class Socket:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def send(self, message):
                transmitted.append(json.loads(message))
                if transmitted[-1]['type'] == 'emg_frame':
                    ready.set()
            async def recv(self):
                return json.dumps({'type': 'hello_ack'})
            def __aiter__(self):
                return self
            async def __anext__(self):
                await asyncio.Event().wait()

        fake_bleak = types.SimpleNamespace(BleakScanner=Scanner, BleakClient=Client)
        with patch.dict('sys.modules', {'bleak': fake_bleak}), patch('websockets.connect', return_value=Socket()):
            task = asyncio.create_task(run('test-device', 'ws://example.test/ws/mudra', None))
            await asyncio.wait_for(ready.wait(), timeout=3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(transmitted[0]['type'], 'hello')
        self.assertEqual(transmitted[1]['samples'][5], [5, -5, 105])
        self.assertEqual(transmitted[1]['device_timestamp_us'], 98765)
        self.assertIn(bytes([6, 0, 0]), writes)
        self.assertIn('stop_notify', writes)


if __name__ == '__main__':
    unittest.main()
