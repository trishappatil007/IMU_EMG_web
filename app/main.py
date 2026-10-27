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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

STATIC = Path(__file__).parent / "static"
SAMPLE_RATE_HZ = 50  # nominal simulator rate, not a guaranteed measured rate
CSV_FIELDS = ("participant_id", "session_id", "gesture", "trial_id", "sensor_model",
              "timestamp_ns", "sequence_number", "ax", "ay", "az", "gx", "gy", "gz")
AXES = ("ax", "ay", "az", "gx", "gy", "gz")


class Trial(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    participant_id: str = Field(min_length=1, max_length=80)
    session_id: str = Field(min_length=1, max_length=80)
    gesture: str = Field(min_length=1, max_length=80)
    trial_id: int = Field(ge=1, strict=True)
    mounting_location: str = Field(default="", max_length=200)
    orientation_notes: str = Field(default="", max_length=500)
    trial_notes: str = Field(default="", max_length=1000)
    accelerometer_range: str = Field(default="", max_length=100)
    gyroscope_range: str = Field(default="", max_length=100)


class StartCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    metadata: Trial
    countdown_seconds: int = Field(default=3, ge=0, le=30, strict=True)


class Acquisition:
    def __init__(self, directory: Path):
        self.directory = directory
        self.clients: set[asyncio.Queue] = set()
        self.metadata = None
        self.handle = None
        self.writer = None
        self.filename = None
        self.sidecar = None
        self.count = 0
        self.sequence = 0
        self.error = None
        self.phase = "idle"
        self.countdown_remaining = None
        self.countdown_task = None
        self.trial_info = None

    def status(self):
        return {"type": "status", "phase": self.phase,
                "recording": self.phase == "recording", "source": "simulated",
                "sensor_model": "simulated_imu", "sample_rate_hz": SAMPLE_RATE_HZ,
                "countdown_remaining": self.countdown_remaining,
                "metadata": self.metadata, "recorded_samples": self.count,
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

    def start(self, command: StartCommand):
        if self.phase != "idle":
            raise ValueError("Finish the current trial or review it before starting another.")
        # Catch invalid/unwritable destination before the countdown begins.
        self.directory.mkdir(parents=True, exist_ok=True)
        self.metadata = command.metadata.model_dump()
        self.filename = self.sidecar = None
        self.count, self.error = 0, None
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
            self.begin_recording()
        except asyncio.CancelledError:
            raise
        except OSError:
            self.phase, self.countdown_remaining = "idle", None
            self.error = "Cannot start recording; check directory permissions and disk space."
            self.publish(self.status())
        finally:
            if self.countdown_task is asyncio.current_task():
                self.countdown_task = None

    def begin_recording(self):
        stem = f"trial-{uuid4().hex}"
        self.filename, self.sidecar = stem + '.csv', stem + '.json'
        path = self.directory / self.filename
        handle = path.open('x', newline='', encoding='utf-8')
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        info = {"schema_version": 1, "csv_file": self.filename, **self.metadata,
                "sensor_model": "simulated_imu", "source": "simulated",
                "sampling_rate_hz": SAMPLE_RATE_HZ, "sampling_rate_type": "nominal",
                "timestamp_clock": "host Unix time", "timestamp_unit": "ns",
                "acceleration_unit": "m/s^2", "angular_velocity_unit": "rad/s",
                "sequence_number_scope": "per trial, starting at zero",
                "review_status": "recording", "recorded_samples": 0}
        try:
            writer.writeheader()
            handle.flush()
            self.write_sidecar(info)
        except OSError:
            handle.close()
            with suppress(OSError):
                path.unlink(missing_ok=True)
            self.filename = self.sidecar = None
            raise
        self.handle, self.writer, self.trial_info = handle, writer, info
        self.phase, self.countdown_remaining = "recording", None
        self.publish(self.status())

    def stop(self, failed=False):
        if self.phase == 'countdown':
            self.countdown_task.cancel()
            self.phase, self.countdown_remaining = 'idle', None
            self.publish(self.status())
            return
        if self.phase != 'recording':
            raise ValueError("There is no active recording or countdown.")
        try:
            self.handle.close()
        except OSError:
            failed = True
        finally:
            self.handle = self.writer = None
        self.phase = 'review'
        self.trial_info['review_status'] = 'needs_review'
        self.trial_info['recorded_samples'] = self.count
        self.trial_info['recording_error'] = bool(failed)
        if failed:
            self.error = 'Recording failed. Reject this trial and check disk space and permissions.'
        try:
            self.write_sidecar(self.trial_info)
        except OSError:
            self.error = 'Could not update metadata. Check disk space and permissions, then review again.'
        self.publish(self.status())

    def review(self, decision):
        if self.phase != 'review':
            raise ValueError('There is no trial awaiting review.')
        if decision not in ('accepted', 'rejected'):
            raise ValueError('Review decision must be accepted or rejected.')
        if decision == 'accepted' and (self.count == 0 or self.trial_info.get('recording_error')):
            raise ValueError('Cannot accept an empty or failed recording. Reject and repeat it.')
        info = {**self.trial_info, 'review_status': decision}
        self.write_sidecar(info)  # only transition when the decision is safely stored
        self.trial_info = info
        self.phase, self.error = 'idle', None
        self.publish(self.status())

    async def simulate(self):
        started = time.monotonic()
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
            if self.phase == 'recording':
                row = {key: self.metadata[key] for key in ('participant_id', 'session_id', 'gesture', 'trial_id')}
                row.update(sensor_model='simulated_imu', timestamp_ns=timestamp_ns, sequence_number=self.count)
                row.update({key: sample[key] for key in AXES})
                try:
                    self.writer.writerow(row)
                    self.handle.flush()
                    self.count += 1
                except OSError:
                    self.stop(failed=True)
            sample["recorded_samples"] = self.count
            self.publish(sample)
            await asyncio.sleep(1 / SAMPLE_RATE_HZ)


def create_app(data_dir: Path | None = None):
    acquisition = Acquisition(data_dir if data_dir is not None else Path(os.getenv("IMU_DATA_DIR", "recordings")))

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(acquisition.simulate())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if acquisition.phase in ('countdown', 'recording'):
            acquisition.stop()

    app = FastAPI(title="LHNT IMU / EMG foundation", lifespan=lifespan)
    app.state.acquisition = acquisition
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    async def health():
        return {"ok": True, **acquisition.status()}

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
                    elif message.get("type") == "stop_recording":
                        acquisition.stop()
                    elif message.get("type") == "review_trial":
                        acquisition.review(message.get('decision'))
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

    return app


app = create_app()
