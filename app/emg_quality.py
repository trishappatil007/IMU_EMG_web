"""Provisional raw EMG trial checks; no device-specific clinical claims."""
import csv
import math
from pathlib import Path

# These settings apply ONLY to the built-in arbitrary-unit simulator. Real EMG
# thresholds and ADC rails must be established with the acquisition team.
SIM_CONFIG = {'name': 'simulator-v1', 'minimum_samples': 500,
              'flatline_std_below': 0.01, 'units': 'simulation units'}


def assess_emg_csv(path: Path, channels: int, expected_count: int, config=None):
    config = dict(config) if config else None
    result = {'version': 1, 'scope': 'raw recorded EMG', 'status': 'review',
              'config': config, 'samples': 0, 'channels': [], 'checks': [],
              'limitations': ['Clipping is not assessed: ADC limits are not configured.',
                              'The metrics cannot establish electrode contact or gesture correctness.']}
    stats = [dict(n=0, mean=0.0, m2=0.0, minimum=None, maximum=None, invalid=0) for _ in range(channels)]
    sequence_errors = 0
    read_error = None
    try:
        with path.open(newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            required = {'sequence_number', 'timestamp_ns', *(f'emg_{i+1}' for i in range(channels))}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError('Missing required EMG CSV columns.')
            for row in reader:
                index = result['samples']
                result['samples'] += 1
                try:
                    if int(row['sequence_number']) != index or int(row['timestamp_ns']) < 0:
                        sequence_errors += 1
                except (ValueError, TypeError, KeyError):
                    sequence_errors += 1
                for i, stat in enumerate(stats):
                    try:
                        x = float(row[f'emg_{i+1}'])
                        if not math.isfinite(x):
                            raise ValueError('non-finite')
                    except (ValueError, TypeError, KeyError):
                        stat['invalid'] += 1
                        continue
                    stat['n'] += 1
                    delta = x - stat['mean']
                    stat['mean'] += delta / stat['n']
                    stat['m2'] += delta * (x - stat['mean'])
                    stat['minimum'] = x if stat['minimum'] is None else min(stat['minimum'], x)
                    stat['maximum'] = x if stat['maximum'] is None else max(stat['maximum'], x)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        read_error = str(exc)
    count_ok = read_error is None and result['samples'] == expected_count and result['samples'] > 0
    result['checks'].append({'name': 'File and sample count', 'status': 'pass' if count_ok else 'review',
                             'detail': read_error or f"{result['samples']} rows read; {expected_count} expected."})
    result['checks'].append({'name': 'Sample sequence and timestamps', 'status': 'pass' if not read_error and result['samples'] and not sequence_errors else 'review',
                             'detail': f'{sequence_errors} rows with missing/invalid timestamps or nonconsecutive sequence numbers. Timing regularity is not assessed.'})
    enough = bool(config and result['samples'] >= config['minimum_samples'])
    duration_status = 'pass' if enough else ('review' if config else 'not assessed')
    result['checks'].append({'name': 'Minimum sample count', 'status': duration_status,
                             'detail': f"{result['samples']} samples; minimum {config['minimum_samples']} ({config['name']})." if config else 'No device-specific minimum configured.'})
    for i, stat in enumerate(stats):
        std = math.sqrt(stat['m2'] / stat['n']) if stat['n'] else None
        rms = math.sqrt(stat['m2'] / stat['n'] + stat['mean']**2) if stat['n'] else None
        if stat['invalid'] or not stat['n']:
            state, detail = 'review', f"{stat['invalid']} invalid or missing values."
        elif not config:
            state, detail = 'not assessed', 'Flatline threshold not configured for this device.'
        elif not enough:
            state, detail = 'review', 'Too few samples for the provisional flatline check.'
        elif std < config['flatline_std_below']:
            state, detail = 'review', f"Standard deviation below {config['flatline_std_below']} {config['units']}."
        else:
            state, detail = 'pass', 'Values present; provisional flatline check passed.'
        result['channels'].append({'channel': f'EMG {i+1}', 'status': state, 'detail': detail,
                                   'std': std, 'rms': rms, 'min': stat['minimum'], 'max': stat['maximum']})
    result['data_integrity'] = bool(count_ok and not sequence_errors and all(stat['invalid'] == 0 and stat['n'] == result['samples'] for stat in stats))
    statuses = [c['status'] for c in result['checks'] + result['channels']]
    if all(state == 'pass' for state in statuses):
        result['status'] = 'pass'
    elif any(state == 'not assessed' for state in statuses) and not any(state == 'review' for state in statuses):
        result['status'] = 'not assessed'
    return result
