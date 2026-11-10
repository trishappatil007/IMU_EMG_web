import base64
import json
import time
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
from tests.test_app import until


def command(destination=None, duration=.15, sample_rate=50):
    return {'type':'start_recording','countdown_seconds':0, 'duration_seconds':duration,
            'requested_sample_rate_hz':sample_rate,
            'metadata':{'participant_id':'P1','session_id':'S1','gesture':'idle','trial_id':1},
            'destination': destination or {'kind':'local'}}


def test_timed_trial_pending_then_approved_to_local(tmp_path):
    pending=tmp_path/'pending'; target=tmp_path/'chosen'
    with TestClient(create_app(pending)) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status')
        ws.send_json(command({'kind':'local','folder':str(target)}))
        until(ws,'status',lambda m:m['phase']=='recording')
        status=until(ws,'status',lambda m:m['phase']=='review')
        assert not target.exists()
        info=json.loads((pending/status['metadata_file']).read_text())
        assert info['stop_reason']=='duration_elapsed' and info['requested_duration_seconds']==.15
        assert info['sampling_rate_hz']==50 and info['requested_sample_rate_hz']==50
        ws.send_json({'type':'review_trial','decision':'accepted','override_reason':'Short simulation trial'})
        saved=until(ws,'status',lambda m:m['phase']=='idle')
        assert saved['export_result']['folder']==str(target)
        assert (target/status['file']).exists() and (target/status['emg_file']).exists()
        info=json.loads((target/status['metadata_file']).read_text())
        assert info['review_status']=='accepted' and info['emg_quality_override_reason']
        trials=client.get('/api/trials').json()['trials']
        assert len(trials)==1 and trials[0]['review_status']=='accepted'
        assert client.get('/api/trials/files/'+status['file']).status_code==200
        assert client.get('/api/trials/files/secret.txt').status_code==404


def test_rejected_trial_never_exported_and_mismatch_rejected(tmp_path):
    target=tmp_path/'exports'
    with TestClient(create_app(tmp_path/'pending')) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status')
        ws.send_json(command(sample_rate=100))
        assert 'must match' in until(ws,'error')['message']
        ws.send_json(command({'kind':'local','folder':str(target)}))
        until(ws,'status',lambda m:m['phase']=='recording')
        status=until(ws,'status',lambda m:m['phase']=='review')
        ws.send_json({'type':'review_trial','decision':'rejected'})
        until(ws,'status',lambda m:m['phase']=='idle')
        assert not target.exists()
        assert json.loads((tmp_path/'pending'/status['metadata_file']).read_text())['review_status']=='rejected'


def test_github_export_atomic_and_failure_stays_pending(tmp_path,monkeypatch):
    calls=[]
    def gh(payload):
        calls.append(payload)
        if len(calls)==1:return {'repository':{'ref':{'target':{'oid':'head123'}}}}
        if len(calls)==2:return {'repository':{'e0':None,'e1':None,'e2':None}}
        return {'createCommitOnBranch':{'commit':{'oid':'commit123','url':'https://github.com/acme/test/commit/commit123'}}}
    monkeypatch.setattr('app.destinations.gh_call',gh)
    with TestClient(create_app(tmp_path)) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status');ws.send_json(command({'kind':'github','repository':'acme/test','branch':'main','path':'data'}))
        until(ws,'status',lambda m:m['phase']=='recording'); status=until(ws,'status',lambda m:m['phase']=='review')
        assert not calls
        ws.send_json({'type':'review_trial','decision':'accepted','override_reason':'Test simulated data'})
        saved=until(ws,'status',lambda m:m['phase']=='idle')
        assert saved['export_result']['commit']=='commit123' and len(calls)==3
        inp=calls[2]['variables']['input']
        assert inp['expectedHeadOid']=='head123' and len(inp['fileChanges']['additions'])==3
        remote_sidecar=next(x for x in inp['fileChanges']['additions'] if x['path'].endswith('.json'))
        assert json.loads(base64.b64decode(remote_sidecar['contents']))['review_status']=='accepted'
        assert json.loads((tmp_path/status['metadata_file']).read_text())['review_status']=='accepted'

    def fail(payload): raise ValueError('No GitHub access')
    monkeypatch.setattr('app.destinations.gh_call',fail)
    with TestClient(create_app(tmp_path/'failed')) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status');ws.send_json(command({'kind':'github','repository':'acme/test'}))
        until(ws,'status',lambda m:m['phase']=='recording'); status=until(ws,'status',lambda m:m['phase']=='review')
        ws.send_json({'type':'review_trial','decision':'accepted','override_reason':'Test simulated data'})
        assert 'No GitHub access' in until(ws,'error')['message']
        assert client.get('/health').json()['phase']=='review'
        assert json.loads((tmp_path/'failed'/status['metadata_file']).read_text())['review_status']=='needs_review'


def test_github_existing_file_blocks_mutation(tmp_path,monkeypatch):
    calls=[]
    def gh(payload):
        calls.append(payload)
        if len(calls)==1:return {'repository':{'ref':{'target':{'oid':'head123'}}}}
        return {'repository':{'e0':{'oid':'exists'},'e1':None,'e2':None}}
    monkeypatch.setattr('app.destinations.gh_call',gh)
    with TestClient(create_app(tmp_path)) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status');ws.send_json(command({'kind':'github','repository':'acme/test'}))
        until(ws,'status',lambda m:m['phase']=='recording');until(ws,'status',lambda m:m['phase']=='review')
        ws.send_json({'type':'review_trial','decision':'accepted','override_reason':'Pilot'})
        assert 'already exists' in until(ws,'error')['message']
        assert len(calls)==2 and client.get('/health').json()['phase']=='review'


def test_corrupt_imu_cannot_be_approved(tmp_path):
    pending=tmp_path/'pending'
    with TestClient(create_app(pending)) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status');ws.send_json(command())
        until(ws,'status',lambda m:m['phase']=='recording')
        status=until(ws,'status',lambda m:m['phase']=='review')
        assert status['imu_quality']['status']=='pass'
        (pending/status['file']).unlink()
        ws.send_json({'type':'review_trial','decision':'accepted','override_reason':'Pilot'})
        assert 'IMU data are missing or corrupt' in until(ws,'error')['message']
        assert json.loads((pending/status['metadata_file']).read_text())['review_status']=='needs_review'


def test_change_destination_at_review_after_failure(tmp_path):
    pending=tmp_path/'pending'; blocked=tmp_path/'blocked'; blocked.write_text('not a directory')
    target=tmp_path/'corrected'
    with TestClient(create_app(pending)) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status');ws.send_json(command({'kind':'local','folder':str(blocked)}))
        until(ws,'status',lambda m:m['phase']=='recording')
        status=until(ws,'status',lambda m:m['phase']=='review')
        ws.send_json({'type':'review_trial','decision':'accepted','override_reason':'Pilot'})
        until(ws,'error')
        assert client.get('/health').json()['phase']=='review'
        ws.send_json({'type':'review_trial','decision':'accepted','override_reason':'Pilot',
                      'destination':{'kind':'local','folder':str(target)}})
        saved=until(ws,'status',lambda m:m['phase']=='idle')
        assert saved['export_result']['folder']==str(target)
        assert (target/status['file']).exists()
