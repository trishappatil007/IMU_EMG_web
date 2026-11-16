"""ESP32 packet validation and USB serial integration without physical hardware."""
import csv
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from app.emg_quality import assess_emg_csv
from app.esp32_serial import MAX_LINE_BYTES, parse_packet
from app.main import create_app


def packet(first, rows):
    return {'type': 'emg', 'version': 1, 'first_sequence': first, 'samples': rows,
            'device_timestamp_us': 123456}


def until(ws, kind, predicate=lambda item: True):
    for _ in range(500):
        item = ws.receive_json()
        if item.get('type') == kind and predicate(item):
            return item
    raise AssertionError(f'No matching {kind} message')


class ESP32ProtocolTests(unittest.TestCase):
    def test_valid_multi_channel_batches_and_invalid_packets(self):
        valid = packet(9, [[0, -12, 13, 14, 15, 16], [17, 18, 19, 20, 21, 22]])
        self.assertEqual(parse_packet(json.dumps(valid).encode(), 6), valid)
        for bad, message in [
            (b'{broken', 'Malformed JSON'),
            (b'not-json', 'Malformed JSON'),
            (b'\xff', 'UTF-8'),
            (b'{"type":"emg","version":1,"first_sequence":0,"samples":[[NaN,2]]}', 'Non-finite'),
            (b'{"type":"emg","type":"emg","version":1,"first_sequence":0,"samples":[[1,2]]}', 'Duplicate'),
            (json.dumps(packet(0, [[1]])).encode(), 'exactly 2'),
            (json.dumps(packet(0, [[1, True]])).encode(), 'integer count'),
            (json.dumps(packet(0, [[1, 2.5]])).encode(), 'integer count'),
            (json.dumps(packet(0, [[1, 2**31]])).encode(), 'integer count'),
            (json.dumps(packet(-1, [[1, 2]])).encode(), 'first_sequence'),
            (b'x' * (MAX_LINE_BYTES + 1), 'exceeds'),
        ]:
            with self.subTest(bad=bad[:80]):
                with self.assertRaisesRegex(ValueError, message):
                    parse_packet(bad, 2)

    def test_flatline_and_device_sequence_checked_during_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trial.csv'
            path.write_text('sequence_number,timestamp_ns,device_sequence,emg_1\n0,10,7,100\n1,10,8,100\n')
            report = assess_emg_csv(path, 1, 2, source_sequence_column='device_sequence', integer_counts=True)
            self.assertTrue(report['data_integrity'])
            self.assertEqual(report['channels'][0]['status'], 'review')
            self.assertIn('No variation', report['channels'][0]['detail'])
            path.write_text('sequence_number,timestamp_ns,device_sequence,emg_1\n0,10,7,100\n1,10,9,101.5\n')
            report = assess_emg_csv(path, 1, 2, source_sequence_column='device_sequence', integer_counts=True)
            self.assertFalse(report['data_integrity'])
            self.assertEqual(report['checks'][2]['status'], 'review')


@unittest.skipUnless(os.name == 'posix' and importlib.util.find_spec('serial'), 'Requires pySerial and a POSIX pseudo-terminal')
class ESP32SerialFlowTests(unittest.TestCase):
    def setUp(self):
        import pty
        self.master, slave = pty.openpty()
        self.port = os.ttyname(slave)
        os.close(slave)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        if self.master is not None:
            os.close(self.master)
        self.temp.cleanup()

    def send_packet(self, data):
        os.write(self.master, json.dumps(data, separators=(',', ':')).encode() + b'\n')

    def connect(self, ws):
        until(ws, 'status')
        ws.send_json({'type': 'set_source', 'source': 'esp32'})
        until(ws, 'status', lambda item: item['source'] == 'esp32')
        ws.send_json({'type': 'connect_esp32', 'port': self.port, 'baudrate': 115200, 'channels': 2})
        until(ws, 'status', lambda item: item['esp32_connected'])

    def start(self, ws):
        ws.send_json({'type': 'start_recording', 'countdown_seconds': 0, 'requested_sample_rate_hz': 250,
                      'metadata': {'participant_id': 'P1', 'session_id': 'S1', 'gesture': 'idle'}})
        until(ws, 'status', lambda item: item['phase'] == 'recording')

    def test_usb_data_is_validated_recorded_and_reviewed(self):
        with TestClient(create_app(self.root)) as client, client.websocket_connect('/ws') as ws:
            self.connect(ws)
            os.write(self.master, b'not valid json\n')
            warning = until(ws, 'status', lambda item: item.get('esp32_error'))
            self.assertIn('Malformed JSON', warning['esp32_error'])
            ws.send_json({'type': 'start_recording', 'countdown_seconds': 0,
                          'metadata': {'participant_id': 'P1', 'session_id': 'S1', 'gesture': 'idle'}})
            self.assertIn('valid EMG packets', until(ws, 'error')['message'])
            raw = json.dumps(packet(10, [[100, 200], [101, 201]])).encode() + b'\n'
            os.write(self.master, raw[:15]); os.write(self.master, raw[15:])
            until(ws, 'status', lambda item: item['esp32_receiving'])
            self.start(ws)
            self.send_packet(packet(12, [[102, 202], [103, 203]]))
            batch = until(ws, 'emg_batch', lambda item: item['source'] == 'esp32' and item['recorded_samples'] == 2)
            self.assertEqual(batch['samples'][1]['values'], [103, 203])
            ws.send_json({'type': 'stop_recording'})
            review = until(ws, 'status', lambda item: item['phase'] == 'review')
            self.assertEqual(review['emg_quality']['status'], 'not assessed')
            self.assertTrue(review['emg_quality']['data_integrity'])
            with (self.root / review['emg_file']).open(newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row['device_sequence'] for row in rows], ['12', '13'])
            self.assertEqual([row['emg_1'] for row in rows], ['102', '103'])
            self.assertEqual(rows[0]['device_timestamp_us'], '123456')
            ws.send_json({'type': 'review_trial', 'decision': 'accepted', 'override_reason': 'Pilot signal review'})
            saved = until(ws, 'status', lambda item: item['phase'] == 'idle')
            self.assertEqual(saved['export_result']['kind'], 'local')
            info = json.loads((self.root / review['metadata_file']).read_text())
            self.assertEqual(info['source'], 'esp32')
            self.assertEqual(info['emg_unit'], 'raw device counts')
            self.assertEqual(info['emg_sample_rate_type'], 'user supplied, unverified')

    def test_malformed_data_invalidates_a_recording(self):
        with TestClient(create_app(self.root)) as client, client.websocket_connect('/ws') as ws:
            self.connect(ws)
            self.send_packet(packet(0, [[100, 200], [101, 201]]))
            until(ws, 'status', lambda item: item['esp32_receiving'])
            self.start(ws)
            self.send_packet(packet(2, [[102, 202], [103, 203]]))
            until(ws, 'emg_batch', lambda item: item['source'] == 'esp32' and item['recorded_samples'] == 2)
            self.send_packet(packet(4, [[1]]))
            review = until(ws, 'status', lambda item: item['phase'] == 'review' and item.get('error') and 'exactly 2' in item['error'])
            self.assertTrue(review['recording_invalid'])
            info = json.loads((self.root / review['metadata_file']).read_text())
            self.assertEqual(info['review_status'], 'invalid')
            self.assertIn('exactly 2', info['recording_error_detail'])
            ws.send_json({'type': 'review_trial', 'decision': 'accepted', 'override_reason': 'try'})
            self.assertIn('failed recording', until(ws, 'error')['message'])

    def test_malformed_data_cancels_the_countdown(self):
        with TestClient(create_app(self.root)) as client, client.websocket_connect('/ws') as ws:
            self.connect(ws)
            self.send_packet(packet(0, [[100, 200], [101, 201]]))
            until(ws, 'status', lambda item: item['esp32_receiving'])
            ws.send_json({'type': 'start_recording', 'countdown_seconds': 3,
                          'metadata': {'participant_id': 'P1', 'session_id': 'S1', 'gesture': 'idle'}})
            until(ws, 'status', lambda item: item['phase'] == 'countdown')
            os.write(self.master, b'{bad json}\n')
            status = until(ws, 'status', lambda item: item['phase'] == 'idle' and item.get('error'))
            self.assertIn('countdown cancelled', status['error'])
            self.assertEqual(list(self.root.glob('trial-*')), [])

    def test_oversized_line_invalidates_a_recording(self):
        with TestClient(create_app(self.root)) as client, client.websocket_connect('/ws') as ws:
            self.connect(ws)
            self.send_packet(packet(0, [[100, 200], [101, 201]]))
            until(ws, 'status', lambda item: item['esp32_receiving'])
            self.start(ws)
            os.write(self.master, b'x' * (MAX_LINE_BYTES + 10) + b'\n')
            review = until(ws, 'status', lambda item: item['phase'] == 'review' and item.get('error'))
            self.assertIn('exceeds', review['error'])
            self.assertTrue(review['recording_invalid'])

    def test_sequence_gap_invalidates_a_recording(self):
        with TestClient(create_app(self.root)) as client, client.websocket_connect('/ws') as ws:
            self.connect(ws)
            self.send_packet(packet(0, [[100, 200], [101, 201]]))
            until(ws, 'status', lambda item: item['esp32_receiving'])
            self.start(ws)
            self.send_packet(packet(4, [[102, 202]]))
            review = until(ws, 'status', lambda item: item['phase'] == 'review' and item.get('error') and 'expected 2, got 4' in item['error'])
            self.assertEqual(review['recorded_samples'], 0)
            self.assertTrue(json.loads((self.root / review['metadata_file']).read_text())['recording_error'])

    def test_disconnect_invalidates_a_recording(self):
        with TestClient(create_app(self.root)) as client, client.websocket_connect('/ws') as ws:
            self.connect(ws)
            self.send_packet(packet(0, [[100, 200], [101, 201]]))
            until(ws, 'status', lambda item: item['esp32_receiving'])
            self.start(ws)
            os.close(self.master)
            self.master = None
            review = until(ws, 'status', lambda item: item['phase'] == 'review' and item.get('error'))
            self.assertIn('serial connection lost', review['error'])
            self.assertTrue(json.loads((self.root / review['metadata_file']).read_text())['recording_error'])

    def test_review_rechecks_the_device_sequence(self):
        with TestClient(create_app(self.root)) as client, client.websocket_connect('/ws') as ws:
            self.connect(ws)
            self.send_packet(packet(0, [[100, 200], [101, 201]]))
            until(ws, 'status', lambda item: item['esp32_receiving'])
            self.start(ws)
            self.send_packet(packet(2, [[102, 202], [103, 203]]))
            until(ws, 'emg_batch', lambda item: item['source'] == 'esp32' and item['recorded_samples'] == 2)
            ws.send_json({'type': 'stop_recording'})
            review = until(ws, 'status', lambda item: item['phase'] == 'review')
            csv_path = self.root / review['emg_file']
            with csv_path.open(newline='') as handle:
                reader = csv.DictReader(handle)
                fieldnames, rows = reader.fieldnames, list(reader)
            rows[1]['device_sequence'] = '99'
            with csv_path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader(); writer.writerows(rows)
            ws.send_json({'type': 'review_trial', 'decision': 'accepted', 'override_reason': 'try'})
            self.assertIn('missing or corrupt', until(ws, 'error')['message'])


if __name__ == '__main__':
    unittest.main()
