import csv
from fastapi.testclient import TestClient
from app.main import create_app


def until(ws, kind, predicate=lambda m: True):
    for _ in range(150):
        message = ws.receive_json()
        if message['type'] == kind and predicate(message):
            return message
    raise AssertionError(f'No {kind} message received')


def test_stream_recording_and_reconnection(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        assert client.get('/health').json()['recording'] is False
        with client.websocket_connect('/ws') as ws:
            assert until(ws, 'status')['recording'] is False
            sample = until(ws, 'sample')
            assert sample['source'] == 'simulated' and sample['az'] > 8
            metadata = dict(participant='P001', session='S001', gesture='flexion', trial=1)
            ws.send_json(dict(type='start_recording', metadata=metadata))
            assert until(ws, 'status')['recording'] is True
            until(ws, 'sample', lambda m: m['recorded_samples'] >= 3)
            ws.send_json(dict(type='start_recording', metadata=metadata))
            assert 'already active' in until(ws, 'error')['message']
        # Recording lives on the backend, not in a browser connection.
        with client.websocket_connect('/ws') as ws:
            assert until(ws, 'status')['recording'] is True
            ws.send_json(dict(type='stop_recording'))
            status = until(ws, 'status')
            assert status['recording'] is False
        rows = list(csv.DictReader((tmp_path / status['file']).open()))
        assert len(rows) >= 3
        assert all(row['participant'] == 'P001' and row['gesture'] == 'flexion' for row in rows)
        assert float(rows[0]['az']) > 8


def test_invalid_commands_and_multiple_viewers(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        with client.websocket_connect('/ws') as a, client.websocket_connect('/ws') as b:
            until(a, 'status'); until(b, 'status')
            for message in [[], {'type': 'bad'}, {'type': 'start_recording', 'metadata': {}},
                            {'type': 'start_recording', 'metadata': dict(participant=' ', session='S1', gesture='g', trial=0)}]:
                a.send_json(message)
                assert until(a, 'error')['message']
            a.send_text('{')
            assert until(a, 'error')['message']
            a.send_json(dict(type='start_recording', metadata=dict(participant='P1', session='S1', gesture='g', trial=1)))
            assert until(b, 'status')['recording'] is True
            assert until(b, 'sample')['source'] == 'simulated'
            b.send_json(dict(type='stop_recording'))
            assert until(a, 'status', lambda m: not m['recording'])['recording'] is False


def test_unwritable_recording_path_stays_idle(tmp_path):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('occupied')
    with TestClient(create_app(blocked)) as client:
        with client.websocket_connect('/ws') as ws:
            until(ws, 'status')
            ws.send_json(dict(type='start_recording', metadata=dict(participant='P1', session='S1', gesture='g', trial=1)))
            assert 'Cannot write' in until(ws, 'error')['message']
            assert client.get('/health').json()['recording'] is False
            assert until(ws, 'sample')['source'] == 'simulated'
