import csv
import json
import time
from fastapi.testclient import TestClient
from app.main import CSV_FIELDS, create_app


def until(ws, kind, predicate=lambda m: True):
    for _ in range(300):
        message = ws.receive_json()
        if message['type'] == kind and predicate(message):
            return message
    raise AssertionError(f'No matching {kind} message received')


def start(ws, countdown=0):
    ws.send_json({'type': 'start_recording', 'countdown_seconds': countdown,
                  'metadata': {'participant_id': 'P001', 'session_id': 'S001',
                               'gesture': 'pronation', 'trial_id': 1,
                               'mounting_location': 'wrist', 'orientation_notes': 'X toward hand'}})


def test_schema_recording_review_and_reconnection(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        with client.websocket_connect('/ws') as ws:
            assert until(ws, 'status')['phase'] == 'idle'
            sample = until(ws, 'sample')
            assert sample['source'] == 'simulated' and sample['az'] > 8
            assert isinstance(sample['timestamp_ns'], str)
            start(ws)
            until(ws, 'status', lambda m: m['phase'] == 'recording')
            until(ws, 'sample', lambda m: m['recorded_samples'] >= 4)
        with client.websocket_connect('/ws') as ws:
            assert until(ws, 'status')['phase'] == 'recording'
            ws.send_json({'type': 'stop_recording'})
            status = until(ws, 'status', lambda m: m['phase'] == 'review')
            assert status['metadata']['participant_id'] == 'P001'
            path = tmp_path / status['file']
            sidecar = tmp_path / status['metadata_file']
            with path.open(newline='') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert reader.fieldnames == list(CSV_FIELDS)
            assert len(rows) >= 4
            assert [int(row['sequence_number']) for row in rows] == list(range(len(rows)))
            assert all(row['participant_id'] == 'P001' and row['sensor_model'] == 'simulated_imu' for row in rows)
            assert all(int(row['timestamp_ns']) > 1_000_000_000_000_000_000 for row in rows)
            assert all(abs(float(row['gy'])) <= 0.262 for row in rows)
            info = json.loads(sidecar.read_text())
            assert info['review_status'] == 'needs_review'
            assert info['mounting_location'] == 'wrist' and info['angular_velocity_unit'] == 'rad/s'
            assert info['recorded_samples'] == len(rows)
            start(ws)
            assert 'review' in until(ws, 'error')['message']
            ws.send_json({'type': 'review_trial', 'decision': 'accepted'})
            assert until(ws, 'status', lambda m: m['phase'] == 'idle')['file'] == status['file']
            assert json.loads(sidecar.read_text())['review_status'] == 'accepted'
            start(ws)
            until(ws, 'status', lambda m: m['phase'] == 'recording')
            until(ws, 'sample', lambda m: m['recorded_samples'] >= 2)
            ws.send_json({'type': 'stop_recording'})
            second = until(ws, 'status', lambda m: m['phase'] == 'review')
            assert second['file'] != status['file']
            ws.send_json({'type': 'review_trial', 'decision': 'rejected'})
            until(ws, 'status', lambda m: m['phase'] == 'idle')
            assert json.loads((tmp_path / second['metadata_file']).read_text())['review_status'] == 'rejected'
            assert (tmp_path / second['file']).exists()


def test_countdown_cancel_and_multiple_viewers(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        with client.websocket_connect('/ws') as a, client.websocket_connect('/ws') as b:
            until(a, 'status'); until(b, 'status')
            start(a, countdown=2)
            assert until(b, 'status', lambda m: m['phase'] == 'countdown')['countdown_remaining'] == 2
            a.send_json({'type': 'stop_recording'})
            until(b, 'status', lambda m: m['phase'] == 'idle')
            time.sleep(0.1)
            assert not list(tmp_path.iterdir())
            start(a, countdown=1)
            assert until(b, 'status', lambda m: m['phase'] == 'recording')['recording']
            b.send_json({'type': 'stop_recording'})
            until(a, 'status', lambda m: m['phase'] == 'review')


def test_invalid_commands_and_unwritable_directory(tmp_path):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('occupied')
    with TestClient(create_app(blocked)) as client:
        with client.websocket_connect('/ws') as ws:
            until(ws, 'status')
            for message in [[], {'type': 'bad'}, {'type': 'start_recording', 'metadata': {}},
                            {'type': 'start_recording', 'countdown_seconds': -1, 'metadata': {}},
                            {'type': 'review_trial', 'decision': 'accepted'}]:
                ws.send_json(message)
                assert until(ws, 'error')['message']
            ws.send_text('{')
            assert until(ws, 'error')['message']
            start(ws)
            assert 'Cannot write' in until(ws, 'error')['message']
            assert client.get('/health').json()['phase'] == 'idle'
            assert until(ws, 'sample')['source'] == 'simulated'


def test_emg_batch_and_raw_companion_csv(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        with client.websocket_connect('/ws') as ws:
            until(ws, 'status')
            batch = until(ws, 'emg_batch')
            assert batch['source'] == 'simulated' and batch['sample_rate_hz'] == 1000
            assert len(batch['samples']) == 20 and len(batch['samples'][0]['values']) == 4
            start(ws)
            until(ws, 'status', lambda m: m['phase'] == 'recording')
            until(ws, 'sample', lambda m: m['recorded_samples'] >= 3)
            ws.send_json({'type': 'stop_recording'})
            status = until(ws, 'status', lambda m: m['phase'] == 'review')
            info = json.loads((tmp_path / status['metadata_file']).read_text())
            with (tmp_path / status['emg_file']).open(newline='') as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == info['emg_recorded_samples'] >= 60
            assert [int(r['sequence_number']) for r in rows] == list(range(len(rows)))
            assert all(r['sensor_model'] == 'simulated_emg' and r['gesture'] == 'pronation' for r in rows)
            assert set(('emg_1', 'emg_2', 'emg_3', 'emg_4')).issubset(rows[0])
            assert info['emg_unit'] == 'arbitrary simulation units'
