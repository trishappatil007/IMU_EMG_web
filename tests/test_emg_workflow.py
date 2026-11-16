"""End-to-end tests for manual EMG collection and approved exports."""
import csv
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.emg_quality import assess_emg_csv
from app.main import StartCommand, Trial, create_app


def until(ws, kind, predicate=lambda item: True):
    for _ in range(500):
        item = ws.receive_json()
        if item.get('type') == kind and predicate(item):
            return item
    raise AssertionError(f'No matching {kind} message')


def command(destination=None):
    return {'type': 'start_recording', 'countdown_seconds': 0,
            'requested_sample_rate_hz': 1000,
            'destination': destination or {'kind': 'local'},
            'metadata': {'participant_id': 'P1', 'session_id': 'S1', 'gesture': 'idle'}}


class WorkflowTests(unittest.TestCase):
    def test_metadata_identifiers_reject_invalid_characters(self):
        with self.assertRaisesRegex(Exception, 'Participant ID must start'):
            Trial(participant_id='../P1', session_id='S1', gesture='idle')
        with self.assertRaisesRegex(Exception, 'Session ID must start'):
            Trial(participant_id='P1', session_id='S 1', gesture='idle')
        self.assertEqual(Trial(participant_id='P_01-A', session_id='Session.2', gesture='idle').participant_id, 'P_01-A')

    def test_sampling_frequency_must_be_positive_and_has_no_5000_hz_limit(self):
        valid = {'type': 'start_recording', 'metadata': {'participant_id': 'P1', 'session_id': 'S1', 'gesture': 'idle'}}
        for rate in (0, -1, float('inf')):
            with self.subTest(rate=rate):
                with self.assertRaises(Exception):
                    StartCommand.model_validate({**valid, 'requested_sample_rate_hz': rate})
        for rate in (1000, 5000.1, 10000, 25000):
            with self.subTest(rate=rate):
                self.assertEqual(
                    StartCommand.model_validate({**valid, 'requested_sample_rate_hz': rate}).requested_sample_rate_hz,
                    rate,
                )

    def test_websocket_returns_field_level_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(create_app(Path(tmp))) as client, client.websocket_connect('/ws') as ws:
                until(ws, 'status')
                bad = command()
                bad['metadata']['participant_id'] = 'bad id!'
                bad['metadata']['gesture'] = 'not-a-gesture'
                ws.send_json(bad)
                error = until(ws, 'error')
                self.assertEqual(error['type'], 'error')
                self.assertEqual({field['field'] for field in error['fields']}, {'Metadata → Participant id', 'Metadata → Gesture'})
                self.assertIn('Please correct the following field(s)', error['message'])
                self.assertEqual(client.get('/health').json()['phase'], 'idle')

    def test_gestures_and_default_countdown(self):
        labels = ['forearm raise', 'forearm lower', 'pronation', 'supination', 'gesture 5', 'gesture 6', 'idle']
        for label in labels:
            self.assertEqual(Trial(participant_id='P1', session_id='S1', gesture=label).gesture, label)
        self.assertEqual(StartCommand(type='start_recording', metadata=Trial(participant_id='P1', session_id='S1', gesture='idle')).countdown_seconds, 3)

    def test_run_countdown_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with TestClient(create_app(root)) as client, client.websocket_connect('/ws') as ws:
                until(ws, 'status')
                request = command()
                del request['countdown_seconds']
                ws.send_json(request)
                countdown = until(ws, 'status', lambda m: m['phase'] == 'countdown')
                self.assertEqual(countdown['countdown_remaining'], 3)
                ws.send_json({'type': 'stop_recording'})
                until(ws, 'status', lambda m: m['phase'] == 'idle')
                self.assertEqual(list(root.glob('trial-*')), [])

    def test_simulated_manual_record_review_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'approved').mkdir()
            with TestClient(create_app(root / 'staging')) as client, client.websocket_connect('/ws') as ws:
                status = until(ws, 'status')
                self.assertEqual(status['sample_rate_hz'], 1000)
                ws.send_json(command({'kind': 'local', 'folder': str(root / 'approved')}))
                until(ws, 'status', lambda m: m['phase'] == 'recording')
                until(ws, 'emg_batch', lambda m: m['recorded_samples'] >= 40)
                time.sleep(.1)
                self.assertEqual(client.get('/health').json()['phase'], 'recording')
                ws.send_json({'type': 'stop_recording'})
                review = until(ws, 'status', lambda m: m['phase'] == 'review')
                self.assertTrue(review['emg_file'].endswith('-emg.csv'))
                self.assertTrue(review['emg_quality']['data_integrity'])
                self.assertEqual(list((root / 'approved').iterdir()), [])
                ws.send_json({'type': 'review_trial', 'decision': 'accepted', 'override_reason': 'Short simulation'})
                saved = until(ws, 'status', lambda m: m['phase'] == 'idle')
                self.assertEqual(len(saved['export_result']['files']), 2)
                info = json.loads((root / 'approved' / review['metadata_file']).read_text())
                self.assertEqual(info['review_status'], 'accepted')
                self.assertEqual(info['source'], 'simulated')
                self.assertTrue(info['trial_id'])
                self.assertNotIn('imu_quality', info)
                with (root / 'approved' / review['emg_file']).open(newline='') as handle:
                    rows = list(csv.DictReader(handle))
                self.assertGreaterEqual(len(rows), 40)
                self.assertEqual(rows[0]['sequence_number'], '0')
                self.assertEqual(client.get('/api/trials/files/' + review['emg_file']).status_code, 200)

    def test_missing_emg_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with TestClient(create_app(root)) as client, client.websocket_connect('/ws') as ws:
                until(ws, 'status'); ws.send_json(command())
                until(ws, 'status', lambda m: m['phase'] == 'recording')
                until(ws, 'emg_batch', lambda m: m['recorded_samples'] >= 40)
                ws.send_json({'type': 'stop_recording'})
                review = until(ws, 'status', lambda m: m['phase'] == 'review')
                (root / review['emg_file']).unlink()
                ws.send_json({'type': 'review_trial', 'decision': 'accepted', 'override_reason': 'test'})
                self.assertIn('missing or corrupt', until(ws, 'error')['message'])
                self.assertEqual(client.get('/health').json()['phase'], 'review')

    def test_github_export_uses_two_files_only_after_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def gh(payload):
                calls.append(payload)
                if len(calls) == 1:
                    return {'repository': {'ref': {'target': {'oid': 'head123'}}}}
                if len(calls) == 2:
                    return {'repository': {'e0': None, 'e1': None}}
                return {'createCommitOnBranch': {'commit': {'oid': 'commit123', 'url': 'https://example.test/commit'}}}
            with patch('app.destinations.gh_call', gh), TestClient(create_app(Path(tmp))) as client, client.websocket_connect('/ws') as ws:
                until(ws, 'status')
                ws.send_json(command({'kind': 'github', 'repository': 'example/data', 'branch': 'main', 'path': 'raw-data'}))
                until(ws, 'status', lambda m: m['phase'] == 'recording')
                until(ws, 'emg_batch', lambda m: m['recorded_samples'] >= 40)
                ws.send_json({'type': 'stop_recording'})
                until(ws, 'status', lambda m: m['phase'] == 'review')
                self.assertEqual(calls, [])
                ws.send_json({'type': 'review_trial', 'decision': 'accepted', 'override_reason': 'Short simulation'})
                saved = until(ws, 'status', lambda m: m['phase'] == 'idle')
                self.assertEqual(saved['export_result']['commit'], 'commit123')
                additions = calls[-1]['variables']['input']['fileChanges']['additions']
                self.assertEqual(len(additions), 2)
                self.assertTrue(all(entry['path'].startswith('raw-data/') for entry in additions))

    def test_unknown_thresholds_remain_not_assessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'emg.csv'
            path.write_text('sequence_number,timestamp_ns,emg_1,emg_2,emg_3\n0,1,10,20,30\n1,1,11,21,31\n')
            report = assess_emg_csv(path, 3, 2, None)
            self.assertTrue(report['data_integrity'])
            self.assertEqual(report['status'], 'not assessed')


if __name__ == '__main__':
    unittest.main()
