"""Local simulated acquisition and trial review; hardware is not connected yet."""
import asyncio
import csv
import json
import math
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4
from typing import Literal

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from app.emg_quality import assess_emg_csv, SIM_CONFIG
from app.destinations import Destination, export
from app.imu_quality import assess_imu_csv

STATIC = Path(__file__).parent / "static"
SAMPLE_RATE_HZ = 50  # approximate publication rate
EMG_RATE_HZ = 1000  # nominal simulated samples/second
EMG_CHANNELS = 4
EMG_BATCH_SIZE = EMG_RATE_HZ // SAMPLE_RATE_HZ
CSV_FIELDS = ("participant_id", "session_id", "gesture", "trial_id", "sensor_model",
              "timestamp_ns", "sequence_number", "ax", "ay", "az", "gx", "gy", "gz")
AXES = ("ax", "ay", "az", "gx", "gy", "gz")


class Trial(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    participant_id: str = Field(min_length=1, max_length=80)
    session_id: str = Field(min_length=1, max_length=80)
    gesture: Literal["forearm raise", "forearm lower", "pronation", "supination", "idle"]
    trial_id: int = Field(ge=1, strict=True)
    mounting_location: str = Field(default="", max_length=200)
    orientation_notes: str = Field(default="", max_length=500)
    trial_notes: str = Field(default="", max_length=1000)
    accelerometer_range: str = Field(default="", max_length=100)
    gyroscope_range: str = Field(default="", max_length=100)


class DeviceHello(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["hello"]
    device_id: str = Field(min_length=1, max_length=100)
    sensor_model: str = Field(min_length=1, max_length=100)
    sample_rate_hz: float = Field(gt=0, le=5000)
    acceleration_unit: Literal["m/s^2"]
    angular_velocity_unit: Literal["rad/s"]
    timestamp_clock: str = Field(min_length=1, max_length=100)


class DeviceSample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["sample"]
    timestamp_ns: int = Field(ge=0, strict=True)
    sequence_number: int = Field(ge=0, strict=True)
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


class Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["prediction"]
    gesture: Literal["forearm raise", "forearm lower", "pronation", "supination", "idle"]
    confidence: float = Field(ge=0, le=1)


class StartCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    metadata: Trial
    countdown_seconds: int = Field(default=3, ge=0, le=30, strict=True)
    duration_seconds: float | None = Field(default=None, gt=0, le=3600, allow_inf_nan=False)
    requested_sample_rate_hz: float | None = Field(default=None, gt=0, le=5000, allow_inf_nan=False)
    destination: Destination = Field(default_factory=Destination)


class Acquisition:
    def __init__(self, directory: Path):
        self.directory = directory
        self.clients: set[asyncio.Queue] = set()
        self.metadata = None
        self.handle = None
        self.writer = None
        self.emg_handle = self.emg_writer = None
        self.emg_filename = None
        self.emg_count = 0
        self.filename = None
        self.sidecar = None
        self.count = 0
        self.sequence = 0
        self.error = None
        self.phase = "idle"
        self.countdown_remaining = None
        self.countdown_task = None
        self.trial_info = None
        self.source = 'simulated'
        self.device = None
        self.device_queue = None
        self.last_device_sample = None
        self.last_device_sequence = None
        self.recording_started = None
        self.finished_elapsed = 0
        self.prediction = None
        self.prediction_at = None
        self.emg_quality = None
        self.imu_quality = None
        self.destination = Destination()
        self.duration_seconds = None
        self.requested_rate = None
        self.timer_task = None
        self.export_result = None

    def status(self):
        return {"type": "status", "phase": self.phase,
                "recording": self.phase == "recording", "source": self.source,
                "device_connected": self.device is not None,
                "device_receiving": self.device is not None and self.last_device_sample is not None and time.monotonic() - self.last_device_sample < 3,
                "device_id": self.device.get('device_id') if self.device else None,
                "sensor_model": self.device['sensor_model'] if self.source == 'pi' and self.device else "simulated_imu",
                "sample_rate_hz": self.device['sample_rate_hz'] if self.source == 'pi' and self.device else SAMPLE_RATE_HZ,
                "elapsed_seconds": time.monotonic() - self.recording_started if self.phase == 'recording' and self.recording_started else self.finished_elapsed,
                "prediction": self.prediction if self.prediction_at is not None and time.monotonic() - self.prediction_at < 3 else None,
                "emg_quality": self.emg_quality, "imu_quality": self.imu_quality,
                "duration_seconds": self.duration_seconds, "requested_sample_rate_hz": self.requested_rate,
                "destination": self.destination.model_dump(), "export_result": self.export_result,
                "countdown_remaining": self.countdown_remaining,
                "metadata": self.metadata, "recorded_samples": self.count,
                "emg_file": self.emg_filename, "emg_recorded_samples": self.emg_count,
                "emg_sample_rate_hz": EMG_RATE_HZ, "emg_channels": EMG_CHANNELS,
                "file": self.filename, "metadata_file": self.sidecar,
                "error": self.error}

    @staticmethod
    def enqueue(queue, message):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(message)

    def publish(self, message):
        for queue in self.clients:
            self.enqueue(queue, message)

    def write_sidecar(self, info):
        # Replace atomically so a failed update does not destroy the last good metadata.
        path = self.directory / self.sidecar
        tmp = path.with_suffix('.json.tmp')
        try:
            tmp.write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
            os.replace(tmp, path)
        finally:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)

    def set_source(self, source):
        if self.phase != 'idle':
            raise ValueError('Finish and review the trial before changing sources.')
        if source not in ('simulated', 'pi'):
            raise ValueError('Source must be simulated or pi.')
        self.source = source
        self.prediction = None
        self.publish(self.status())

    def device_command(self, message):
        if self.device_queue is None:
            return
        if self.device_queue.full():
            raise ValueError('Device command queue is full; reconnect the Pi.')
        self.device_queue.put_nowait(message)

    def start(self, command: StartCommand):
        if self.phase != "idle":
            raise ValueError("Finish the current trial or review it before starting another.")
        if self.source == 'pi' and (not self.device or self.last_device_sample is None or time.monotonic() - self.last_device_sample >= 3):
            raise ValueError('Pi must be connected and sending recent samples before recording.')
        actual_rate = self.device['sample_rate_hz'] if self.source == 'pi' else SAMPLE_RATE_HZ
        if command.requested_sample_rate_hz is not None and abs(command.requested_sample_rate_hz - actual_rate) > 1e-6:
            raise ValueError(f'Requested sample rate must match the source-reported rate ({actual_rate} Hz); this app cannot configure the sensor yet.')
        self.destination = command.destination
        self.duration_seconds = command.duration_seconds
        self.requested_rate = command.requested_sample_rate_hz
        self.export_result = None
        self.finished_elapsed = 0
        # Catch invalid/unwritable destination before the countdown begins.
        self.directory.mkdir(parents=True, exist_ok=True)
        self.metadata = command.metadata.model_dump()
        self.filename = self.sidecar = self.emg_filename = None
        self.count, self.emg_count, self.error = 0, 0, None
        self.emg_quality = self.imu_quality = None
        self.phase = "countdown"
        self.countdown_remaining = command.countdown_seconds
        self.publish(self.status())
        self.countdown_task = asyncio.create_task(self.countdown(command.countdown_seconds))

    async def countdown(self, seconds):
        try:
            for remaining in range(seconds, 0, -1):
                self.countdown_remaining = remaining
                self.publish(self.status())
                await asyncio.sleep(1)
            if self.source == 'pi' and (not self.device or self.last_device_sample is None or time.monotonic() - self.last_device_sample >= 3):
                raise ValueError('Pi disconnected or stopped sending samples during countdown.')
            self.begin_recording()
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError) as exc:
            self.phase, self.countdown_remaining = "idle", None
            self.error = str(exc) if isinstance(exc, ValueError) else "Cannot start recording; check directory permissions and disk space."
            self.publish(self.status())
        finally:
            if self.countdown_task is asyncio.current_task():
                self.countdown_task = None

    def begin_recording(self):
        stem = f"trial-{uuid4().hex}"
        self.filename, self.sidecar, self.emg_filename = stem + '.csv', stem + '.json', stem + '-emg.csv'
        if self.source == 'pi':
            self.emg_filename = None
        path = self.directory / self.filename
        handle = path.open('x', newline='', encoding='utf-8')
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        emg_handle = None
        info = {"schema_version": 1, "csv_file": self.filename, **self.metadata,
                "sensor_model": "simulated_imu", "source": "simulated",
                "sampling_rate_hz": SAMPLE_RATE_HZ, "sampling_rate_type": "nominal",
                "timestamp_clock": "host Unix time", "timestamp_unit": "ns",
                "acceleration_unit": "m/s^2", "angular_velocity_unit": "rad/s",
                "sequence_number_scope": "per trial, starting at zero",
                "review_status": "recording", "recorded_samples": 0,
                "requested_duration_seconds": self.duration_seconds,
                "requested_sample_rate_hz": self.requested_rate,
                "export_destination": self.destination.model_dump(),
                "emg_csv_file": self.emg_filename, "emg_sensor_model": "simulated_emg",
                "emg_sample_rate_hz": EMG_RATE_HZ, "emg_sample_rate_type": "nominal",
                "emg_channel_count": EMG_CHANNELS, "emg_unit": "arbitrary simulation units",
                "emg_timestamps": "synthetic, host-anchored batches", "emg_recorded_samples": 0}
        if self.source == 'pi':
            info.update(sensor_model=self.device['sensor_model'], source='pi',
                        sampling_rate_hz=self.device['sample_rate_hz'],
                        timestamp_clock=self.device['timestamp_clock'],
                        sequence_number_scope='device stream counter',
                        device_id=self.device['device_id'], emg_csv_file=None,
                        emg_sensor_model=None, emg_sample_rate_hz=None, emg_channel_count=0)
        try:
            emg_writer = None
            if self.emg_filename is not None:
                emg_handle = (self.directory / self.emg_filename).open('x', newline='', encoding='utf-8')
                emg_writer = csv.DictWriter(emg_handle, fieldnames=('participant_id', 'session_id', 'gesture', 'trial_id', 'sensor_model', 'timestamp_ns', 'sequence_number', *(f'emg_{i}' for i in range(1, EMG_CHANNELS + 1))))
            writer.writeheader()
            if emg_writer is not None:
                emg_writer.writeheader()
            handle.flush()
            if emg_handle is not None:
                emg_handle.flush()
            self.write_sidecar(info)
        except OSError:
            handle.close()
            if emg_handle is not None:
                emg_handle.close()
            with suppress(OSError):
                path.unlink(missing_ok=True)
                if self.emg_filename is not None:
                    (self.directory / self.emg_filename).unlink(missing_ok=True)
            self.filename = self.sidecar = self.emg_filename = None
            raise
        self.handle, self.writer, self.trial_info = handle, writer, info
        self.emg_handle, self.emg_writer = emg_handle, emg_writer
        self.phase, self.countdown_remaining = "recording", None
        self.recording_started = time.monotonic()
        if self.source == 'pi':
            try:
                self.device_command({'type': 'start_recording', 'metadata': self.metadata, 'duration_seconds': self.duration_seconds, 'requested_sample_rate_hz': self.requested_rate})
            except ValueError:
                self.stop(failed=True)
                return
        if self.phase == 'recording' and self.duration_seconds is not None:
            self.timer_task = asyncio.create_task(self.auto_stop(self.duration_seconds))
        self.publish(self.status())

    async def auto_stop(self, duration):
        try:
            await asyncio.sleep(duration)
            if self.phase == 'recording':
                self.stop(stop_reason='duration_elapsed')
        except asyncio.CancelledError:
            pass
        finally:
            if self.timer_task is asyncio.current_task():
                self.timer_task = None

    def stop(self, failed=False, stop_reason='operator_stop'):
        if self.phase == 'countdown':
            self.countdown_task.cancel()
            self.phase, self.countdown_remaining = 'idle', None
            self.publish(self.status())
            return
        if self.phase != 'recording':
            raise ValueError("There is no active recording or countdown.")
        if self.timer_task and self.timer_task is not asyncio.current_task():
            self.timer_task.cancel()
            self.timer_task = None
        elapsed = time.monotonic() - self.recording_started if self.recording_started else None
        self.finished_elapsed = elapsed or 0
        try:
            self.handle.close()
        except OSError:
            failed = True
        finally:
            self.handle = self.writer = None
        try:
            if self.emg_handle is not None:
                self.emg_handle.close()
        except OSError:
            failed = True
        finally:
            self.emg_handle = self.emg_writer = None
        if self.source == 'pi' and self.device_queue is not None:
            with suppress(ValueError):
                self.device_command({'type': 'stop_recording'})
        self.phase = 'review'
        self.trial_info['review_status'] = 'invalid' if failed else 'needs_review'
        self.trial_info['recorded_samples'] = self.count
        self.trial_info['emg_recorded_samples'] = self.emg_count
        self.trial_info['recording_error'] = bool(failed)
        self.trial_info['actual_duration_seconds'] = elapsed
        self.trial_info['stop_reason'] = 'interrupted' if failed else stop_reason
        self.imu_quality = assess_imu_csv(self.directory / self.filename, self.count)
        self.trial_info['imu_quality'] = self.imu_quality
        if self.emg_filename:
            self.emg_quality = assess_emg_csv(self.directory / self.emg_filename, EMG_CHANNELS,
                                              self.emg_count, SIM_CONFIG)
            self.trial_info['emg_quality'] = self.emg_quality
        else:
            self.emg_quality = {'status': 'not collected', 'channels': [], 'checks': [],
                                'limitations': ['This IMU-only source did not record EMG.']}
            self.trial_info['emg_quality'] = self.emg_quality
        if failed:
            self.error = 'Recording failed. Reject this trial and check disk space and permissions.'
        try:
            self.write_sidecar(self.trial_info)
        except OSError:
            self.error = 'Could not update metadata. Check disk space and permissions, then review again.'
        self.publish(self.status())

    async def review(self, decision, override_reason="", destination=None):
        if self.phase != 'review':
            raise ValueError('There is no trial awaiting review.')
        if decision not in ('accepted', 'rejected'):
            raise ValueError('Review decision must be accepted or rejected.')
        if decision == 'accepted' and (self.count == 0 or (self.source == 'simulated' and self.emg_count == 0) or self.trial_info.get('recording_error')):
            raise ValueError('Cannot accept an empty or failed recording. Reject and repeat it.')
        if decision == 'accepted' and destination is not None:
            self.destination = Destination.model_validate(destination)
        if not isinstance(override_reason, str) or len(override_reason) > 1000:
            raise ValueError('Override reason must be text of at most 1000 characters.')
        # Recheck the saved raw files at acceptance, not just the live plot.
        if decision == 'accepted':
            self.imu_quality = assess_imu_csv(self.directory / self.filename, self.count)
            self.trial_info['imu_quality'] = self.imu_quality
            if not self.imu_quality.get('data_integrity'):
                self.publish(self.status())
                raise ValueError('IMU data are missing or corrupt. Reject this trial and repeat it.')
        if decision == 'accepted' and self.emg_filename:
            self.emg_quality = assess_emg_csv(self.directory / self.emg_filename, EMG_CHANNELS,
                                              self.emg_count, SIM_CONFIG)
            self.trial_info['emg_quality'] = self.emg_quality
            if not self.emg_quality.get('data_integrity'):
                self.publish(self.status())
                raise ValueError('EMG data are missing or corrupt. Reject this trial and repeat it.')
            if self.emg_quality['status'] != 'pass' and not override_reason.strip():
                self.publish(self.status())
                raise ValueError('EMG checks need review. Enter a reason to accept anyway, or reject the trial.')
        info = {**self.trial_info, 'review_status': ('invalid' if self.trial_info.get('recording_error') else decision)}
        if decision == 'accepted':
            info['export_destination'] = self.destination.model_dump()
        if decision == 'accepted' and self.emg_filename and self.emg_quality['status'] != 'pass':
            info['emg_quality_override_reason'] = override_reason.strip()
        if decision == 'accepted':
            # Incomplete/failed exports leave the source trial in review for retry.
            self.phase = 'saving'
            self.error = None
            self.publish(self.status())
            try:
                files = {self.filename:(self.directory/self.filename).read_bytes()}
                if self.emg_filename:
                    files[self.emg_filename] = (self.directory/self.emg_filename).read_bytes()
                files[self.sidecar] = (json.dumps(info, indent=2) + '\n').encode('utf-8')
                result = await asyncio.to_thread(export, self.destination, self.directory, files)
                self.write_sidecar(info)
                self.export_result = result
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.phase = 'review'
                self.error = str(exc)[:500] or 'Export failed. The trial remains pending review.'
                self.publish(self.status())
                raise ValueError(self.error) from exc
        else:
            self.write_sidecar(info)
        self.trial_info = info
        self.phase, self.error = 'idle', None
        self.publish(self.status())

    def ingest_device_sample(self, sample):
        last = self.last_device_sequence
        was_receiving = self.last_device_sample is not None
        self.last_device_sample = time.monotonic()
        if not was_receiving:
            self.publish(self.status())
        if last is not None and sample.sequence_number != last + 1:
            if self.source == 'pi' and self.phase == 'recording':
                self.stop(failed=True)
                self.error = f'Pi sequence gap or out-of-order sample: expected {last + 1}, received {sample.sequence_number}. Trial invalid.'
                self.publish(self.status())
            if sample.sequence_number <= last:
                return
        self.last_device_sequence = sample.sequence_number
        if self.source != 'pi':
            return
        values = sample.model_dump()
        if self.phase == 'recording':
            row = {key: self.metadata[key] for key in ('participant_id', 'session_id', 'gesture', 'trial_id')}
            row.update(sensor_model=self.device['sensor_model'], timestamp_ns=sample.timestamp_ns, sequence_number=sample.sequence_number)
            row.update({key: values[key] for key in AXES})
            try:
                self.writer.writerow(row)
                self.handle.flush()
                self.count += 1
            except OSError:
                self.stop(failed=True)
        self.publish({'type':'sample', 'source':'pi', 'sequence':sample.sequence_number,
                      'timestamp_ns':str(sample.timestamp_ns), 'recorded_samples':self.count,
                      **{key:values[key] for key in AXES}})

    async def simulate(self):
        started = time.monotonic()
        emg_sequence = 0
        last_prediction_at = 0
        while True:
            t = time.monotonic() - started
            self.sequence += 1
            timestamp_ns = time.time_ns()
            sample = {"type": "sample", "source": "simulated", "sequence": self.sequence,
                      # String prevents JavaScript from rounding 19-digit timestamps.
                      "timestamp_ns": str(timestamp_ns),
                      "ax": round(math.sin(t * 2), 5), "ay": round(math.cos(t * 1.5), 5),
                      "az": round(9.81 + math.sin(t), 5),
                      "gx": round(math.radians(20) * math.sin(t), 5),
                      "gy": round(math.radians(15) * math.cos(t), 5),
                      "gz": round(math.radians(10) * math.sin(t * .5), 5)}
            # Generate batches on a nominal 1 kHz simulation grid. Timestamps are
            # synthetic within each host-anchored batch, not physical acquisition times.
            batch = []
            for i in range(EMG_BATCH_SIZE):
                sample_time = emg_sequence / EMG_RATE_HZ
                envelope = .35 + .25 * (1 + math.sin(2 * math.pi * .35 * sample_time))
                channels = [round(envelope * (math.sin(2 * math.pi * (45 + 13 * ch) * sample_time + ch)
                            + .4 * math.sin(2 * math.pi * (120 + 17 * ch) * sample_time))
                            + .18 * math.sin(2 * math.pi * 60 * sample_time), 5)
                            for ch in range(EMG_CHANNELS)]
                batch.append({"timestamp_ns": str(timestamp_ns - (EMG_BATCH_SIZE - 1 - i) * 1_000_000),
                              "values": channels})
                emg_sequence += 1
            if self.phase == 'recording' and self.source == 'simulated':
                row = {key: self.metadata[key] for key in ('participant_id', 'session_id', 'gesture', 'trial_id')}
                row.update(sensor_model='simulated_imu', timestamp_ns=timestamp_ns, sequence_number=self.count)
                row.update({key: sample[key] for key in AXES})
                try:
                    self.writer.writerow(row)
                    self.handle.flush()
                    self.count += 1
                    for entry in batch:
                        emg_row = {key: self.metadata[key] for key in ('participant_id', 'session_id', 'gesture', 'trial_id')}
                        emg_row.update(sensor_model='simulated_emg', timestamp_ns=entry['timestamp_ns'], sequence_number=self.emg_count)
                        emg_row.update({f'emg_{i + 1}': value for i, value in enumerate(entry['values'])})
                        self.emg_writer.writerow(emg_row)
                        self.emg_count += 1
                    self.emg_handle.flush()
                except OSError:
                    self.stop(failed=True)
            sample["recorded_samples"] = self.count
            if self.source == 'simulated':
                if time.monotonic() - last_prediction_at >= 1:
                    self.prediction = {'type':'prediction', 'gesture':'idle', 'confidence':0.75, 'source':'simulated'}
                    self.prediction_at = time.monotonic()
                    self.publish(self.prediction)
                    last_prediction_at = time.monotonic()
                self.publish(sample)
                self.publish({"type": "emg_batch", "source": "simulated", "sample_rate_hz": EMG_RATE_HZ,
                              "first_sequence": emg_sequence - EMG_BATCH_SIZE, "samples": batch})
            if self.device is not None and self.last_device_sample is not None and time.monotonic() - self.last_device_sample >= 3:
                if self.source == 'pi' and self.phase == 'recording':
                    self.stop(failed=True)
                    self.error = 'Pi sample stream timed out. Trial marked invalid.'
                elif self.source == 'pi' and self.phase == 'countdown':
                    self.stop()
                    self.error = 'Pi sample stream timed out; countdown cancelled.'
                self.last_device_sample = None
                self.prediction = None
                self.publish(self.status())
            await asyncio.sleep(1 / SAMPLE_RATE_HZ)


def create_app(data_dir: Path | None = None):
    acquisition = Acquisition(data_dir if data_dir is not None else Path(os.getenv("IMU_DATA_DIR", "recordings")))

    @asynccontextmanager
    async def lifespan(app):
        # A sidecar left recording after process loss is never accepted silently.
        if acquisition.directory.exists():
            for path in acquisition.directory.glob('trial-*.json'):
                try:
                    info = json.loads(path.read_text(encoding='utf-8'))
                    if info.get('review_status') == 'recording':
                        info['review_status'] = 'invalid'
                        info['recording_error'] = True
                        info['recovery_note'] = 'Server restarted before the trial ended.'
                        tmp = path.with_suffix('.json.tmp')
                        tmp.write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
                        os.replace(tmp, path)
                except (OSError, json.JSONDecodeError):
                    # Unreadable sidecars must not be treated as accepted.
                    pass
        task = asyncio.create_task(acquisition.simulate())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if acquisition.phase in ('countdown', 'recording'):
            acquisition.stop(failed=acquisition.phase == 'recording')

    app = FastAPI(title="LHNT IMU / EMG foundation", lifespan=lifespan)
    app.state.acquisition = acquisition
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    async def health():
        return {"ok": True, **acquisition.status()}

    @app.get('/api/trials')
    async def list_trials():
        trials = []
        if acquisition.directory.exists():
            for path in sorted(acquisition.directory.glob('trial-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:500]:
                try:
                    info = json.loads(path.read_text(encoding='utf-8'))
                    trials.append({key:info.get(key) for key in ('participant_id','session_id','gesture','trial_id','review_status','csv_file','emg_csv_file','sensor_model','source','recorded_samples') } | {'metadata_file':path.name})
                except (OSError, json.JSONDecodeError):
                    continue
        return {'trials':trials}

    @app.get('/api/trials/files/{filename}')
    async def trial_file(filename: str):
        import re
        from fastapi import HTTPException
        if not re.fullmatch(r'trial-[0-9a-f]{32}(?:-emg)?\.(?:csv|json)', filename):
            raise HTTPException(status_code=404)
        path = acquisition.directory / filename
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, filename=filename)

    @app.websocket("/ws")
    async def stream(ws: WebSocket):
        await ws.accept()
        queue = asyncio.Queue(maxsize=100)
        queue.put_nowait(acquisition.status())
        acquisition.clients.add(queue)

        async def send():
            while True:
                await ws.send_json(await queue.get())

        sender = asyncio.create_task(send())
        try:
            while True:
                try:
                    message = await ws.receive_json()
                    if not isinstance(message, dict):
                        raise ValueError("Command must be a JSON object.")
                    if message.get("type") == "start_recording":
                        acquisition.start(StartCommand.model_validate(message))
                    elif message.get("type") == "set_source":
                        acquisition.set_source(message.get('source'))
                    elif message.get("type") == "stop_recording":
                        acquisition.stop()
                    elif message.get("type") == "review_trial":
                        await acquisition.review(message.get('decision'), message.get('override_reason', ''), message.get('destination'))
                    elif message.get("type") == "get_status":
                        acquisition.enqueue(queue, acquisition.status())
                    else:
                        raise ValueError("Unknown command.")
                except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    acquisition.enqueue(queue, {"type": "error", "message": str(exc)})
                except OSError:
                    acquisition.enqueue(queue, {"type": "error", "message": "Cannot write recording; check directory permissions and disk space."})
        except WebSocketDisconnect:
            pass
        finally:
            acquisition.clients.discard(queue)
            sender.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await sender

    @app.websocket('/ws/device')
    async def device_stream(ws: WebSocket):
        await ws.accept()
        if acquisition.device is not None:
            await ws.send_json({'type':'error', 'message':'A Pi is already connected.'})
            await ws.close(code=1008)
            return
        registered = False
        sender = None
        try:
            hello = DeviceHello.model_validate(await asyncio.wait_for(ws.receive_json(), timeout=5))
            # The session is reserved immediately after the first validated hello.
            if acquisition.device is not None:
                await ws.send_json({'type':'error', 'message':'A Pi is already connected.'})
                await ws.close(code=1008)
                return
            acquisition.device = hello.model_dump()
            acquisition.device_queue = asyncio.Queue(maxsize=20)
            acquisition.last_device_sequence = acquisition.last_device_sample = None
            registered = True
            acquisition.publish(acquisition.status())
            await ws.send_json({'type':'hello_ack', 'message':'Connected. Send timestamped IMU samples.'})
            async def send_commands():
                while True:
                    await ws.send_json(await acquisition.device_queue.get())
            sender = asyncio.create_task(send_commands())
            while True:
                try:
                    message = await ws.receive_json()
                    if not isinstance(message, dict):
                        raise ValueError('Message must be an object.')
                    kind = message.get('type')
                    if kind == 'sample':
                        acquisition.ingest_device_sample(DeviceSample.model_validate(message))
                    elif kind == 'sample_batch':
                        samples = message.get('samples')
                        if set(message) != {'type', 'samples'} or not isinstance(samples, list) or not 1 <= len(samples) <= 500:
                            raise ValueError('sample_batch requires 1 to 500 samples.')
                        validated = [DeviceSample.model_validate({'type':'sample', **entry}) for entry in samples]
                        for sample in validated:
                            acquisition.ingest_device_sample(sample)
                    elif kind == 'prediction':
                        prediction = Prediction.model_validate(message).model_dump()
                        prediction['source'] = 'pi'
                        prediction['device_id'] = acquisition.device['device_id']
                        if acquisition.source == 'pi':
                            acquisition.prediction = prediction
                            acquisition.prediction_at = time.monotonic()
                            acquisition.publish(prediction)
                    elif kind == 'error':
                        raise ValueError('Pi reported an error: ' + str(message.get('message', 'unknown'))[:200])
                    else:
                        raise ValueError('Unknown device message type.')
                except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    if acquisition.source == 'pi' and acquisition.phase == 'recording':
                        acquisition.stop(failed=True)
                        acquisition.error = 'Invalid Pi message; trial marked invalid.'
                        acquisition.publish(acquisition.status())
                    await ws.send_json({'type':'error', 'message':str(exc)[:500]})
        except (WebSocketDisconnect, asyncio.TimeoutError, ValidationError, ValueError, json.JSONDecodeError):
            pass
        finally:
            if sender:
                sender.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await sender
            if registered:
                acquisition.device = acquisition.device_queue = None
                acquisition.last_device_sequence = acquisition.last_device_sample = None
                acquisition.prediction = None
                if acquisition.source == 'pi' and acquisition.phase == 'recording':
                    acquisition.stop(failed=True)
                    acquisition.error = 'Pi disconnected. Trial marked invalid.'
                elif acquisition.source == 'pi' and acquisition.phase == 'countdown':
                    acquisition.stop()
                    acquisition.error = 'Pi disconnected; countdown cancelled.'
                acquisition.publish(acquisition.status())

    return app


app = create_app()

