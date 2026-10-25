"""Local IMU acquisition foundation; replace the simulator with a sensor adapter."""
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


class Trial(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    participant: str = Field(min_length=1, max_length=80)
    session: str = Field(min_length=1, max_length=80)
    gesture: str = Field(min_length=1, max_length=80)
    trial: int = Field(ge=1, strict=True)


class Acquisition:
    def __init__(self, directory: Path):
        self.directory = directory
        self.clients: set[asyncio.Queue] = set()
        self.metadata = None
        self.handle = None
        self.writer = None
        self.filename = None
        self.count = 0
        self.sequence = 0
        self.error = None

    def status(self):
        return {"type": "status", "recording": self.handle is not None,
                "source": "simulated", "sample_rate_hz": 50,
                "metadata": self.metadata, "recorded_samples": self.count,
                "file": self.filename, "error": self.error}

    def publish(self, message):
        # Bound each client's backlog; a slow viewer cannot stall acquisition.
        for queue in self.clients:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    def start(self, metadata: Trial):
        if self.handle is not None:
            raise ValueError("A recording is already active. Stop it first.")
        self.directory.mkdir(parents=True, exist_ok=True)
        filename = f"trial-{uuid4().hex}.csv"
        handle = (self.directory / filename).open("x", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        try:
            writer.writerow(["participant", "session", "gesture", "trial", "source",
                             "sequence", "timestamp_ms", "ax", "ay", "az", "gx", "gy", "gz"])
            handle.flush()
        except OSError:
            handle.close()
            raise
        self.filename, self.metadata = filename, metadata.model_dump()
        self.handle, self.writer, self.count, self.error = handle, writer, 0, None
        self.publish(self.status())

    def stop(self):
        if self.handle is not None:
            try:
                self.handle.close()
            finally:
                self.handle = self.writer = None
        self.publish(self.status())

    async def simulate(self):
        started = time.monotonic()
        while True:
            t = time.monotonic() - started
            self.sequence += 1
            sample = {"type": "sample", "source": "simulated", "sequence": self.sequence,
                      "timestamp_ms": time.time_ns() // 1_000_000,
                      "ax": round(math.sin(t * 2), 5), "ay": round(math.cos(t * 1.5), 5),
                      "az": round(9.81 + math.sin(t), 5),
                      "gx": round(20 * math.sin(t), 5), "gy": round(15 * math.cos(t), 5),
                      "gz": round(10 * math.sin(t * .5), 5)}
            if self.handle is not None:
                try:
                    self.writer.writerow([*self.metadata.values(), "simulated",
                                          *[sample[k] for k in ("sequence", "timestamp_ms", "ax", "ay", "az", "gx", "gy", "gz")]])
                    self.handle.flush()
                    self.count += 1
                except OSError:
                    self.error = "Recording failed: check disk space and directory permissions."
                    with suppress(OSError):
                        self.stop()
            sample["recorded_samples"] = self.count
            self.publish(sample)
            await asyncio.sleep(.02)


def create_app(data_dir: Path | None = None):
    acquisition = Acquisition(data_dir if data_dir is not None else Path(os.getenv("IMU_DATA_DIR", "recordings")))

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(acquisition.simulate())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        with suppress(OSError):
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
                        acquisition.start(Trial.model_validate(message.get("metadata")))
                    elif message.get("type") == "stop_recording":
                        acquisition.stop()
                    elif message.get("type") == "get_status":
                        queue.put_nowait(acquisition.status())
                    else:
                        raise ValueError("Unknown command.")
                except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    queue.put_nowait({"type": "error", "message": str(exc)})
                except OSError:
                    queue.put_nowait({"type": "error", "message": "Cannot write recording; check directory permissions and disk space."})
        except WebSocketDisconnect:
            pass
        finally:
            acquisition.clients.discard(queue)
            sender.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await sender

    return app


app = create_app()
