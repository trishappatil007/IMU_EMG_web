import csv
import json
import math
from app.emg_quality import assess_emg_csv, SIM_CONFIG
from fastapi.testclient import TestClient
from app.main import create_app
from tests.test_app import until, start


def create_data(path, kind='wave', count=600):
    with path.open('w', newline='') as f:
        writer=csv.writer(f); writer.writerow(['timestamp_ns','sequence_number','emg_1','emg_2'])
        for i in range(count):
            x=math.sin(i*.3) if kind != 'flat' else 0.0
            writer.writerow([1000000000+i*1000000, i if kind != 'gap' else i+1, x, x*.5])


def test_valid_flat_and_unknown_thresholds(tmp_path):
    path=tmp_path/'emg.csv'; create_data(path)
    report=assess_emg_csv(path,2,600,SIM_CONFIG)
    assert report['status']=='pass' and all(c['status']=='pass' for c in report['channels'])
    assert 'Clipping is not assessed' in report['limitations'][0]
    unknown=assess_emg_csv(path,2,600)
    assert unknown['status']=='not assessed' and unknown['channels'][0]['status']=='not assessed'
    create_data(path,'flat')
    flat=assess_emg_csv(path,2,600,SIM_CONFIG)
    assert flat['status']=='review' and 'below' in flat['channels'][0]['detail']
    create_data(path,'gap')
    assert assess_emg_csv(path,2,600,SIM_CONFIG)['status']=='review'
    assert assess_emg_csv(tmp_path/'missing.csv',2,600,SIM_CONFIG)['status']=='review'


def test_short_trial_requires_reason_and_saves_report(tmp_path):
    with TestClient(create_app(tmp_path)) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status');start(ws)
        until(ws,'status',lambda m:m['phase']=='recording')
        until(ws,'sample',lambda m:m['recorded_samples']>=3)
        ws.send_json({'type':'stop_recording'})
        status=until(ws,'status',lambda m:m['phase']=='review')
        assert status['emg_quality']['status']=='review'
        assert len(status['emg_quality']['channels'])==4
        ws.send_json({'type':'review_trial','decision':'accepted'})
        assert 'Enter a reason' in until(ws,'error')['message']
        ws.send_json({'type':'review_trial','decision':'accepted','override_reason':'Pilot test only'})
        until(ws,'status',lambda m:m['phase']=='idle')
        info=json.loads((tmp_path/status['metadata_file']).read_text())
        assert info['review_status']=='accepted' and info['emg_quality']['status']=='review'
        assert info['emg_quality_override_reason']=='Pilot test only'


def test_sufficient_simulated_samples_pass(tmp_path):
    with TestClient(create_app(tmp_path)) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status');start(ws)
        until(ws,'status',lambda m:m['phase']=='recording')
        until(ws,'sample',lambda m:m['recorded_samples']>=26)
        ws.send_json({'type':'stop_recording'})
        status=until(ws,'status',lambda m:m['phase']=='review')
        assert status['emg_quality']['status']=='pass'
        ws.send_json({'type':'review_trial','decision':'accepted'})
        until(ws,'status',lambda m:m['phase']=='idle')
        info=json.loads((tmp_path/status['metadata_file']).read_text())
        assert info['emg_quality']['status']=='pass'


def test_missing_emg_cannot_be_accepted_with_override(tmp_path):
    with TestClient(create_app(tmp_path)) as client, client.websocket_connect('/ws') as ws:
        until(ws,'status'); start(ws); until(ws,'status',lambda m:m['phase']=='recording')
        until(ws,'sample',lambda m:m['recorded_samples']>=3)
        ws.send_json({'type':'stop_recording'})
        status=until(ws,'status',lambda m:m['phase']=='review')
        (tmp_path/status['emg_file']).unlink()
        ws.send_json({'type':'review_trial','decision':'accepted','override_reason':'Just a test'})
        assert 'missing or corrupt' in until(ws,'error')['message']
        assert json.loads((tmp_path/status['metadata_file']).read_text())['review_status']=='needs_review'
