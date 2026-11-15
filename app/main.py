"""EMG collection, review, and approved exports for simulation and Mudra Link."""
import asyncio
import csv
import json
import math
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
import re
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator, model_validator

from app.destinations import Destination, export, resolve_local_destination
from app.emg_quality import SIM_CONFIG, assess_emg_csv

STATIC = Path(__file__).parent / "static"
SIM_RATE = 1000
SIM_BATCH = 20
SIM_CHANNELS = 4
MUDRA_CHANNELS = 3
MUDRA_SAMPLES_PER_FRAME = 18


class Trial(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    participant_id: str = Field(min_length=1, max_length=80)
    session_id: str = Field(min_length=1, max_length=80)
    gesture: Literal["forearm raise", "forearm lower", "pronation", "supination", "gesture 5", "gesture 6", "idle"]

    @field_validator("participant_id", "session_id")
    @classmethod
    def validate_identifier(cls, value, info):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
            label = "Participant ID" if info.field_name == "participant_id" else "Session ID"
            raise ValueError(
                f"{label} must start with a letter or number and contain only "
                "letters, numbers, periods, underscores, or hyphens (max 80 characters)."
            )
        return value


class StartCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["start_recording"]
    metadata: Trial
    countdown_seconds: int = Field(default=3, ge=0, le=30, strict=True)
    requested_sample_rate_hz: float | None = Field(default=None, gt=0, le=5000, strict=True)
    destination: Destination = Field(default_factory=Destination)

    @field_validator("requested_sample_rate_hz")
    @classmethod
    def validate_sample_rate(cls, value):
        if value is not None and (not math.isfinite(value) or value <= 0 or value > 5000):
            raise ValueError("Sampling frequency must be a finite number greater than 0 and no more than 5000 Hz.")
        return value


class MudraHello(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["hello"]
    device_id: str = Field(min_length=1, max_length=100)
    sample_rate_hz: float | None = Field(default=None, gt=0, le=5000)


class MudraFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["emg_frame"]
    frame_sequence: int = Field(ge=0, strict=True)
    device_timestamp_us: int = Field(ge=0, le=0xFFFFFFFF, strict=True)
    host_received_ns: int = Field(ge=0, strict=True)
    samples: list[tuple[StrictInt, StrictInt, StrictInt]] = Field(
        min_length=MUDRA_SAMPLES_PER_FRAME, max_length=MUDRA_SAMPLES_PER_FRAME
    )

    @model_validator(mode="after")
    def valid_counts(self):
        if any(not -32768 <= value <= 32767 for row in self.samples for value in row):
            raise ValueError("Mudra values must be signed 16-bit raw counts.")
        return self


class Acquisition:
    def __init__(self, directory: Path):
        self.directory = directory
        self.clients: set[asyncio.Queue] = set()
        self.source = "simulated"
        self.mudra = None
        self.last_mudra_sample = None
        self.last_mudra_frame = None
        self.mudra_sample_sequence = 0
        self.phase = "idle"
        self.countdown_remaining = None
        self.countdown_task = None
        self.handle = self.writer = None
        self.metadata = None
        self.filename = self.sidecar = None
        self.trial_info = None
        self.count = 0
        self.started_at = None
        self.finished_elapsed = 0
        self.requested_rate = None
        self.quality = None
        self.destination = Destination()
        self.export_result = None
        self.destination_warning = None
        self.error = None

    def receiving(self):
        return (self.mudra is not None and self.last_mudra_sample is not None
                and time.monotonic() - self.last_mudra_sample < 3)

    def source_rate(self):
        return SIM_RATE if self.source == "simulated" else (self.mudra["sample_rate_hz"] if self.mudra else None)

    def status(self):
        return {
            "type": "status", "phase": self.phase, "recording": self.phase == "recording",
            "source": self.source, "mudra_connected": self.mudra is not None,
            "mudra_receiving": self.receiving(), "mudra_id": self.mudra["device_id"] if self.mudra else None,
            "sample_rate_hz": self.source_rate(), "requested_sample_rate_hz": self.requested_rate,
            "emg_channels": MUDRA_CHANNELS if self.source == "mudra" else SIM_CHANNELS,
            "recorded_samples": self.count, "emg_recorded_samples": self.count,
            "elapsed_seconds": (time.monotonic() - self.started_at if self.phase == "recording" and self.started_at
                                else self.finished_elapsed),
            "countdown_remaining": self.countdown_remaining, "metadata": self.metadata,
            "emg_quality": self.quality, "emg_file": self.filename, "metadata_file": self.sidecar,
            "destination": self.destination.model_dump(), "destination_warning": self.destination_warning,\n            "export_result": self.export_result, "error": self.error,
        }

    @staticmethod
    def enqueue(queue, message):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(message)

    def publish(self, message):
        for queue in self.clients:
            self.enqueue(queue, message)

    def write_sidecar(self, info):
        path = self.directory / self.sidecar
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        finally:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)

    def set_source(self, source):
        if self.phase != "idle":
            raise ValueError("Finish and review the current trial before changing sources.")
        if source not in ("simulated", "mudra"):
            raise ValueError("Choose simulated EMG or Mudra Link EMG.")
        self.source = source
        self.error = None
        self.publish(self.status())

    def start(self, command: StartCommand):
        if self.phase != "idle":
            raise ValueError("Finish and review the current trial before starting another.")
        if self.source == "mudra" and not self.receiving():
            raise ValueError("Connect the Mudra bridge and wait for EMG samples before recording.")
        source_rate = self.source_rate()
        if (command.requested_sample_rate_hz is not None and source_rate is not None
                and abs(command.requested_sample_rate_hz - source_rate) > 1e-6):
            raise ValueError(f"Sampling rate must match the source rate ({source_rate} Hz); the app does not configure the sensor.")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.metadata = command.metadata.model_dump()
        self.metadata["trial_id"] = uuid4().hex
        self.destination = command.destination
        self.destination_warning = None
        if self.destination.kind == "local":
            _, self.destination_warning = resolve_local_destination(self.destination, self.directory)
        self.requested_rate = command.requested_sample_rate_hz or source_rate
        self.filename = self.sidecar = None
        self.count = 0
        self.quality = self.export_result = self.error = None
        self.finished_elapsed = 0
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
            if self.source == "mudra" and not self.receiving():
                raise ValueError("Mudra stopped sending EMG during the countdown.")
            self.begin_recording()
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError) as exc:
            self.phase, self.countdown_remaining = "idle", None
            self.error = str(exc) if isinstance(exc, ValueError) else "Cannot start recording; check disk space and folder permissions."
            self.publish(self.status())
        finally:
            if self.countdown_task is asyncio.current_task():
                self.countdown_task = None

    def begin_recording(self):
        stem = f"trial-{self.metadata['trial_id']}"
        self.filename, self.sidecar = stem + "-emg.csv", stem + ".json"
        channels = MUDRA_CHANNELS if self.source == "mudra" else SIM_CHANNELS
        extra = ("frame_sequence", "device_timestamp_us", "sample_in_frame") if self.source == "mudra" else ()
        columns = ("participant_id", "session_id", "gesture", "trial_id", "sensor_model", "timestamp_ns",
                   "sequence_number", *extra, *(f"emg_{i}" for i in range(1, channels + 1)))
        handle = None
        info = {
            "schema_version": 2, **self.metadata, "source": self.source, "emg_csv_file": self.filename,
            "emg_sensor_model": "mudra_link" if self.source == "mudra" else "simulated_emg",
            "emg_channel_count": channels, "emg_sample_rate_hz": self.requested_rate,
            "emg_sample_rate_type": ("user supplied, unverified" if self.source == "mudra" and self.requested_rate
                                     else "unknown" if self.source == "mudra" else "nominal"),
            "emg_unit": "raw signed 16-bit counts" if self.source == "mudra" else "arbitrary simulation units",
            "emg_timestamps": ("timestamp_ns is host receipt time repeated per frame; device_timestamp_us is a 32-bit per-frame device clock"
                               if self.source == "mudra" else "synthetic host-anchored batches"),
            "emg_channel_names": ["ulnar", "median", "radial"] if self.source == "mudra" else [f"EMG {i}" for i in range(1, channels + 1)],
            "emg_sequence_note": ("Frame sequence is bridge-generated and cannot detect Bluetooth packet loss."
                                  if self.source == "mudra" else "Sample sequence is local to this trial."),
            "device_id": self.mudra["device_id"] if self.source == "mudra" else "simulated",
            "review_status": "recording", "recorded_samples": 0, "emg_recorded_samples": 0,
            "export_destination": self.destination.model_dump(),
        }
        try:
            handle = (self.directory / self.filename).open("x", newline="", encoding="utf-8")
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            handle.flush()
            self.write_sidecar(info)
        except OSError:
            if handle:
                handle.close()
            with suppress(OSError):
                (self.directory / self.filename).unlink(missing_ok=True)
            self.filename = self.sidecar = None
            raise
        self.handle, self.writer, self.trial_info = handle, writer, info
        self.phase, self.countdown_remaining = "recording", None
        self.started_at = time.monotonic()
        self.publish(self.status())

    def assess(self):
        channels = MUDRA_CHANNELS if self.source == "mudra" else SIM_CHANNELS
        result = assess_emg_csv(self.directory / self.filename, channels, self.count,
                                None if self.source == "mudra" else SIM_CONFIG)
        if self.source == "mudra":
            result["limitations"].append("Frame counters are generated by the bridge; Bluetooth packet loss is not measured.")
        return result

    def stop(self, failed=False):
        if self.phase == "countdown":
            if self.countdown_task:
                self.countdown_task.cancel()
            self.phase, self.countdown_remaining = "idle", None
            self.publish(self.status())
            return
        if self.phase != "recording":
            raise ValueError("There is no active recording or countdown.")
        elapsed = time.monotonic() - self.started_at if self.started_at else 0
        self.finished_elapsed = elapsed
        try:
            self.handle.close()
        except OSError:
            failed = True
        finally:
            self.handle = self.writer = None
        self.phase = "review"
        self.trial_info.update(review_status="invalid" if failed else "needs_review",
                               recorded_samples=self.count, emg_recorded_samples=self.count,
                               recording_error=bool(failed), actual_duration_seconds=elapsed,
                               stop_reason="interrupted" if failed else "operator_stop")
        self.quality = self.assess()
        self.trial_info["emg_quality"] = self.quality
        if failed:
            self.error = "Recording interrupted. Reject this trial and check the connection or disk."
        try:
            self.write_sidecar(self.trial_info)
        except OSError:
            self.error = "Could not update metadata. Check disk space and permissions."
        self.publish(self.status())

    async def review(self, decision, override_reason="", destination=None):
        if self.phase != "review":
            raise ValueError("There is no trial awaiting review.")
        if decision not in ("accepted", "rejected"):
            raise ValueError("Review decision must be accepted or rejected.")
        if decision == "accepted" and (self.count == 0 or self.trial_info.get("recording_error")):
            raise ValueError("Cannot accept an empty or failed recording. Reject and repeat it.")
        if decision == "accepted" and destination is not None:
            self.destination = Destination.model_validate(destination)
        if not isinstance(override_reason, str) or len(override_reason) > 1000:
            raise ValueError("Override reason must be text of at most 1000 characters.")
        if decision == "accepted":
            self.quality = self.assess()
            self.trial_info["emg_quality"] = self.quality
            if not self.quality.get("data_integrity"):
                self.publish(self.status())
                raise ValueError("EMG data are missing or corrupt. Reject and repeat the trial.")
            if self.quality["status"] != "pass" and not override_reason.strip():
                self.publish(self.status())
                raise ValueError("EMG checks need review. Enter a reason to accept anyway, or reject the trial.")
        info = {**self.trial_info, "review_status": "invalid" if self.trial_info.get("recording_error") else decision}
        if decision == "accepted":
            info["export_destination"] = self.destination.model_dump()
            if self.quality["status"] != "pass":
                info["emg_quality_override_reason"] = override_reason.strip()
            self.phase, self.error = "saving", None
            self.publish(self.status())
            try:
                files = {self.filename: (self.directory / self.filename).read_bytes(),
                         self.sidecar: (json.dumps(info, indent=2) + "\n").encode("utf-8")}
                result = await asyncio.to_thread(export, self.destination, self.directory, files)
                self.write_sidecar(info)
                self.export_result = result
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.phase = "review"
                self.error = str(exc)[:500] or "Export failed. The trial remains pending review."
                self.publish(self.status())
                raise ValueError(self.error) from exc
        else:
            self.write_sidecar(info)
        self.trial_info = info
        self.phase, self.error = "idle", None
        self.publish(self.status())

    def write_samples(self, samples, timestamp_ns, frame=None):
        try:
            for index, values in enumerate(samples):
                row = dict(self.metadata)
                row.update(sensor_model="mudra_link" if frame else "simulated_emg",
                           timestamp_ns=timestamp_ns[index] if isinstance(timestamp_ns, list) else timestamp_ns,
                           sequence_number=self.count)
                if frame:
                    row.update(frame_sequence=frame.frame_sequence, device_timestamp_us=frame.device_timestamp_us,
                               sample_in_frame=index)
                row.update({f"emg_{i + 1}": value for i, value in enumerate(values)})
                self.writer.writerow(row)
                self.count += 1
            self.handle.flush()
        except OSError:
            self.stop(failed=True)

    def ingest_mudra_frame(self, frame: MudraFrame):
        last = self.last_mudra_frame
        if last is not None and frame.frame_sequence != last + 1:
            raise ValueError(f"Mudra bridge frame sequence gap: expected {last + 1}, got {frame.frame_sequence}.")
        self.last_mudra_frame = frame.frame_sequence
        was_receiving = self.last_mudra_sample is not None
        self.last_mudra_sample = time.monotonic()
        if not was_receiving:
            self.publish(self.status())
        first_sequence = self.mudra_sample_sequence
        self.mudra_sample_sequence += len(frame.samples)
        if self.source != "mudra":
            return
        if self.phase == "recording":
            self.write_samples(frame.samples, frame.host_received_ns, frame)
        self.publish({"type": "emg_batch", "source": "mudra",
                      "sample_rate_hz": self.source_rate(), "first_sequence": first_sequence,
                      "recorded_samples": self.count,
                      "samples": [{"timestamp_ns": str(frame.host_received_ns), "values": values}
                                  for values in frame.samples]})

    async def simulate(self):
        sequence = 0
        while True:
            now_ns = time.time_ns()
            batch, timestamps = [], []
            for i in range(SIM_BATCH):
                sample_time = sequence / SIM_RATE
                envelope = .35 + .25 * (1 + math.sin(2 * math.pi * .35 * sample_time))
                batch.append([round(envelope * (math.sin(2 * math.pi * (45 + 13 * ch) * sample_time + ch)
                              + .4 * math.sin(2 * math.pi * (120 + 17 * ch) * sample_time))
                              + .18 * math.sin(2 * math.pi * 60 * sample_time), 5)
                              for ch in range(SIM_CHANNELS)])
                timestamps.append(now_ns - (SIM_BATCH - 1 - i) * 1_000_000)
                sequence += 1
            if self.phase == "recording" and self.source == "simulated":
                self.write_samples(batch, timestamps)
            if self.source == "simulated":
                self.publish({"type": "emg_batch", "source": "simulated", "sample_rate_hz": SIM_RATE,
                              "first_sequence": sequence - SIM_BATCH, "recorded_samples": self.count,
                              "samples": [{"timestamp_ns": str(ts), "values": values}
                                          for ts, values in zip(timestamps, batch)]})
            if self.mudra is not None and self.last_mudra_sample is not None and not self.receiving():
                if self.source == "mudra" and self.phase == "recording":
                    self.stop(failed=True)
                    self.error = "Mudra EMG stream timed out. Trial marked invalid."
                elif self.source == "mudra" and self.phase == "countdown":
                    self.stop()
                    self.error = "Mudra EMG stream timed out; countdown cancelled."
                self.last_mudra_sample = None
                self.publish(self.status())
            await asyncio.sleep(SIM_BATCH / SIM_RATE)


def validation_error_payload(exc: ValidationError):
    """Convert Pydantic errors into concise field-level messages for the UI."""
    fields = []
    for item in exc.errors(include_url=False):
        location = " → ".join(str(part).replace("_", " ").capitalize() for part in item["loc"])
        detail = item["msg"]
        if detail.startswith("Value error, "):
            detail = detail[len("Value error, "):]
        fields.append({"field": location or "Request", "message": detail})
    message = "Please correct the following field(s): " + " ".join(
        f"{item['field']}: {item['message']}" for item in fields
    )
    return {"type": "error", "message": message, "fields": fields}


def create_app(data_dir: Path | None = None):
    acquisition = Acquisition(data_dir if data_dir is not None else Path(os.getenv("EMG_DATA_DIR", "recordings")))

    @asynccontextmanager
    async def lifespan(app):
        if acquisition.directory.exists():
            for path in acquisition.directory.glob("trial-*.json"):
                try:
                    info = json.loads(path.read_text(encoding="utf-8"))
                    if info.get("review_status") == "recording":
                        info.update(review_status="invalid", recording_error=True,
                                    recovery_note="Server restarted before the trial ended.")
                        tmp = path.with_suffix(".json.tmp")
                        tmp.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
                        os.replace(tmp, path)
                except (OSError, json.JSONDecodeError):
                    pass
        task = asyncio.create_task(acquisition.simulate())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if acquisition.phase in ("countdown", "recording"):
            acquisition.stop(failed=acquisition.phase == "recording")

    app = FastAPI(title="LHNT EMG collection", lifespan=lifespan)
    app.state.acquisition = acquisition
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    async def health():
        return {"ok": True, **acquisition.status()}

    @app.get("/api/trials")
    async def list_trials():
        trials = []
        if acquisition.directory.exists():
            for path in sorted(acquisition.directory.glob("trial-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:500]:
                try:
                    info = json.loads(path.read_text(encoding="utf-8"))
                    trials.append({key: info.get(key) for key in
                                   ("participant_id", "session_id", "gesture", "trial_id", "review_status",
                                    "emg_csv_file", "source", "recorded_samples")} | {"metadata_file": path.name})
                except (OSError, json.JSONDecodeError):
                    continue
        return {"trials": trials}

    @app.get("/api/trials/files/{filename}")
    async def trial_file(filename: str):
        import re
        from fastapi import HTTPException
        if not re.fullmatch(r"trial-[0-9a-f]{32}(?:-emg)?\.(?:csv|json)", filename):
            raise HTTPException(404)
        path = acquisition.directory / filename
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=filename)

    @app.websocket("/ws")
    async def browser_stream(ws: WebSocket):
        await ws.accept()
        queue = asyncio.Queue(maxsize=100)
        acquisition.clients.add(queue)
        acquisition.enqueue(queue, acquisition.status())

        async def send_messages():
            while True:
                await ws.send_json(await queue.get())

        sender = asyncio.create_task(send_messages())
        try:
            while True:
                try:
                    message = await ws.receive_json()
                    if not isinstance(message, dict):
                        raise ValueError("Message must be an object.")
                    kind = message.get("type")
                    if kind == "start_recording":
                        acquisition.start(StartCommand.model_validate(message))
                    elif kind == "stop_recording":
                        acquisition.stop()
                    elif kind == "set_source":
                        acquisition.set_source(message.get("source"))
                    elif kind == "review_trial":
                        await acquisition.review(message.get("decision"), message.get("override_reason", ""),
                                                 message.get("destination"))
                    elif kind == "get_status":
                        acquisition.enqueue(queue, acquisition.status())
                    else:
                        raise ValueError("Unknown command.")
                except ValidationError as exc:
                    acquisition.enqueue(queue, validation_error_payload(exc))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    acquisition.enqueue(queue, {"type": "error", "message": str(exc), "fields": []})
                except OSError:
                    acquisition.enqueue(queue, {"type": "error", "message": "Cannot write recording; check disk space and folder permissions."})
        except WebSocketDisconnect:
            pass
        finally:
            acquisition.clients.discard(queue)
            sender.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await sender

    @app.websocket("/ws/mudra")
    async def mudra_stream(ws: WebSocket):
        await ws.accept()
        if acquisition.mudra is not None:
            await ws.send_json({"type": "error", "message": "A Mudra bridge is already connected."})
            await ws.close(code=1008)
            return
        registered = False
        try:
            hello = MudraHello.model_validate(await asyncio.wait_for(ws.receive_json(), timeout=5))
            if acquisition.mudra is not None:
                await ws.close(code=1008)
                return
            acquisition.mudra = hello.model_dump()
            acquisition.last_mudra_sample = acquisition.last_mudra_frame = None
            acquisition.mudra_sample_sequence = 0
            registered = True
            acquisition.publish(acquisition.status())
            await ws.send_json({"type": "hello_ack", "message": "Mudra bridge connected."})
            while True:
                try:
                    message = await ws.receive_json()
                    if not isinstance(message, dict):
                        raise ValueError("Mudra message must be an object.")
                    if message.get("type") == "error":
                        raise ValueError("Mudra bridge reported: " + str(message.get("message", "unknown"))[:200])
                    acquisition.ingest_mudra_frame(MudraFrame.model_validate(message))
                except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    if acquisition.source == "mudra" and acquisition.phase == "recording":
                        acquisition.stop(failed=True)
                        acquisition.error = "Mudra stream error; trial marked invalid. " + str(exc)[:200]
                        acquisition.publish(acquisition.status())
                    await ws.send_json({"type": "error", "message": str(exc)[:500]})
                    await ws.close(code=1008)
                    break
        except (WebSocketDisconnect, asyncio.TimeoutError, ValidationError, ValueError, json.JSONDecodeError):
            pass
        finally:
            if registered:
                acquisition.mudra = None
                acquisition.last_mudra_sample = acquisition.last_mudra_frame = None
                if acquisition.source == "mudra" and acquisition.phase == "recording":
                    acquisition.stop(failed=True)
                    acquisition.error = "Mudra bridge disconnected. Trial marked invalid."
                elif acquisition.source == "mudra" and acquisition.phase == "countdown":
                    acquisition.stop()
                    acquisition.error = "Mudra disconnected; countdown cancelled."
                acquisition.publish(acquisition.status())

    return app


app = create_app()
