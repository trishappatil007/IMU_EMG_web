"""ESP32 USB serial protocol and pySerial helpers.

Packets are newline-delimited UTF-8 JSON.  Parsing happens in the backend,
before a packet is displayed or written to a trial.
"""
import json

MAX_LINE_BYTES = 8192
MAX_CHANNELS = 6
MAX_BATCH_SAMPLES = 64
MIN_COUNT = -(2**31)
MAX_COUNT = 2**31 - 1
MAX_SEQUENCE = 2**53 - 1  # Exactly representable in the browser's JSON numbers.


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}.")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Non-finite JSON value {value} is not allowed.")


def parse_packet(raw: bytes, channels: int):
    """Validate an entire serial line and return the untouched integer data."""
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError(f"Serial line exceeds {MAX_LINE_BYTES} bytes.")
    try:
        text = raw.decode('utf-8').strip()
    except UnicodeDecodeError as exc:
        raise ValueError('Serial data must be UTF-8 JSON, one packet per line.') from exc
    try:
        packet = json.loads(text, parse_constant=_invalid_constant, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError('Malformed JSON; send one complete EMG packet per line.') from exc
    except RecursionError as exc:
        raise ValueError('JSON packet is nested too deeply.') from exc
    if not isinstance(packet, dict):
        raise ValueError('EMG packet must be a JSON object.')
    allowed = {'type', 'version', 'first_sequence', 'samples', 'device_timestamp_us'}
    unexpected = packet.keys() - allowed
    if unexpected:
        raise ValueError('Unexpected EMG packet fields: ' + ', '.join(sorted(unexpected)) + '.')
    if packet.get('type') != 'emg' or not _integer(packet.get('version')) or packet['version'] != 1:
        raise ValueError('EMG packets must have type "emg" and version 1.')
    sequence = packet.get('first_sequence')
    if not _integer(sequence) or not 0 <= sequence <= MAX_SEQUENCE:
        raise ValueError('first_sequence must be a non-negative integer.')
    samples = packet.get('samples')
    if not isinstance(samples, list) or not 1 <= len(samples) <= MAX_BATCH_SAMPLES:
        raise ValueError(f'samples must contain 1–{MAX_BATCH_SAMPLES} sample rows.')
    if sequence + len(samples) - 1 > MAX_SEQUENCE:
        raise ValueError('Sample sequence exceeds the supported range.')
    for index, row in enumerate(samples):
        if not isinstance(row, list) or len(row) != channels:
            raise ValueError(f'Sample {index + 1} must have exactly {channels} channel values.')
        for channel, value in enumerate(row):
            if not _integer(value) or not MIN_COUNT <= value <= MAX_COUNT:
                raise ValueError(f'Sample {index + 1}, channel {channel + 1} must be a signed 32-bit integer count.')
    timestamp = packet.get('device_timestamp_us')
    if timestamp is not None and (not _integer(timestamp) or not 0 <= timestamp <= MAX_SEQUENCE):
        raise ValueError('device_timestamp_us must be a non-negative integer when supplied.')
    return packet


def open_serial_port(port: str, baudrate: int):
    try:
        import serial
    except ImportError as exc:
        raise ValueError('USB serial support is missing. Install the project requirements.') from exc
    try:
        return serial.Serial(port=port, baudrate=baudrate, timeout=0.15)
    except (OSError, ValueError) as exc:
        raise ValueError(f'Could not open {port}. Check the port, cable, and whether another app is using it. {exc}') from exc


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise ValueError('USB serial support is missing. Install the project requirements.') from exc
    try:
        return [{'device': item.device, 'description': item.description} for item in list_ports.comports()]
    except OSError as exc:
        raise ValueError('Could not list serial ports. Try entering the port manually.') from exc
