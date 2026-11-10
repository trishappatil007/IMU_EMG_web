"""Check file integrity without inventing sensor-specific quality thresholds."""
import csv
import math

AXES=('ax','ay','az','gx','gy','gz')


def assess_imu_csv(path, expected_count):
    result={'status':'review', 'data_integrity':False, 'samples':0, 'checks':[], 'channels':[],
            'limitations':['These checks do not assess clipping, orientation, gesture correctness, sample rate accuracy, or sensor calibration.']}
    stats={a:{'invalid':0,'min':None,'max':None} for a in AXES}
    sequence_errors=timestamp_errors=0
    previous_seq=previous_ts=None
    read_error=None
    try:
        with path.open(newline='', encoding='utf-8') as f:
            rows=csv.DictReader(f)
            if not rows.fieldnames or not {'sequence_number','timestamp_ns',*AXES}.issubset(rows.fieldnames):
                raise ValueError('Missing required IMU columns.')
            for row in rows:
                result['samples']+=1
                try:
                    seq=int(row['sequence_number'])
                    if seq<0 or (previous_seq is not None and seq != previous_seq+1): sequence_errors+=1
                    previous_seq=seq
                except (ValueError,TypeError,KeyError): sequence_errors+=1
                try:
                    ts=int(row['timestamp_ns'])
                    if ts<0 or (previous_ts is not None and ts<=previous_ts): timestamp_errors+=1
                    previous_ts=ts
                except (ValueError,TypeError,KeyError): timestamp_errors+=1
                for axis in AXES:
                    try:
                        x=float(row[axis])
                        if not math.isfinite(x): raise ValueError('nonfinite')
                    except (ValueError,TypeError,KeyError):
                        stats[axis]['invalid']+=1
                        continue
                    stat=stats[axis]
                    stat['min']=x if stat['min'] is None else min(stat['min'],x)
                    stat['max']=x if stat['max'] is None else max(stat['max'],x)
    except (OSError,UnicodeError,csv.Error,ValueError) as exc: read_error=str(exc)
    count_ok=read_error is None and result['samples']>0 and result['samples']==expected_count
    result['checks']=[
        {'name':'File and sample count', 'status':'pass' if count_ok else 'review', 'detail':read_error or f"{result['samples']} rows; {expected_count} expected."},
        {'name':'Sequence numbers', 'status':'pass' if count_ok and sequence_errors==0 else 'review', 'detail':f'{sequence_errors} missing, duplicate, or out-of-order sequence values.'},
        {'name':'Timestamps', 'status':'pass' if count_ok and timestamp_errors==0 else 'review', 'detail':f'{timestamp_errors} missing, duplicate, or out-of-order timestamps. Timing regularity is not assessed.'}]
    for axis, stat in stats.items():
        passed=count_ok and stat['invalid']==0 and stat['min'] is not None
        result['channels'].append({'channel':axis.upper(),'status':'pass' if passed else 'review',
                                   'detail':f"{stat['invalid']} invalid values; range {stat['min']} to {stat['max']}."})
    result['data_integrity']=bool(count_ok and sequence_errors==0 and timestamp_errors==0 and all(c['status']=='pass' for c in result['channels']))
    result['status']='pass' if result['data_integrity'] else 'review'
    return result
