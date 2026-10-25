# IMU / EMG web foundation

Local FastAPI + WebSocket acquisition app for Longhorn Neurotech. No sensor,
Arduino/ESP32, Node build, or frontend framework is needed. The app generates
**simulated IMU data** on the backend at approximately 50 Hz and broadcasts it
through `/ws` to the browser. EMG acquisition is not implemented yet.

## Setup and run

Use Python **3.10 or newer**. Clone this repository and open a terminal inside it:

```bash
git clone https://github.com/trishappatil007/IMU_EMG_web.git
cd IMU_EMG_web
python -m venv .venv
```

Activate the environment:

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Windows Command Prompt (also works if PowerShell blocks activation):

```bat
.venv\Scripts\activate.bat
```

macOS / Linux (use `python3` above if needed):

```bash
source .venv/bin/activate
```

Then, in the activated environment:

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** in your browser. Leave the terminal running.
Stop the server with Ctrl+C. If port 8000 is occupied, add `--port 8001` instead
and open http://127.0.0.1:8001. Run commands from the repository root, not `app/`.

## Try a complete trial

1. Confirm **Connected**, **Idle**, and **SIMULATED IMU** appear. The plot and
   sample counter should update immediately, before recording starts.
2. Enter participant `P001`, session `S001`, gesture `wrist flexion`, trial `1`.
3. Click **Start recording**. Wait for the backend-confirmed **Recording** state
   and increasing recorded-sample count. Metadata is locked during recording.
4. Click **Stop recording**. Confirm **Idle**. Find the displayed CSV filename
   in the server's `recordings/` directory.
5. Open the CSV: it contains metadata, source, sequence, Unix timestamp in
   milliseconds, acceleration `ax/ay/az` in m/s², and gyro `gx/gy/gz` in °/s.

Every recording creates a unique file, so repeated trial labels cannot overwrite
prior recordings. Files are flushed each sample and excluded from git. To change
the output location, set `IMU_DATA_DIR` before starting Uvicorn. The directory is
created on the first recording. Avoid using identifying names for participants.

## Architecture and protocol

- `app/main.py`: FastAPI app, validated trial metadata, simulator, CSV writer,
  shared recording state, and bounded per-viewer WebSocket queues.
- `app/static/`: browser form, live plot, connection/recording status, reconnect.
- `tests/test_app.py`: stream, validation, recordings, reconnect, multiple viewers.
- `GET /health`: current backend status; `GET /docs`: generated HTTP API docs.
- `WS /ws`: bidirectional commands and streaming events.

Browser → backend commands:

```json
{"type":"start_recording","metadata":{"participant":"P001","session":"S001","gesture":"flexion","trial":1}}
{"type":"stop_recording"}
{"type":"get_status"}
```

Backend → browser events:

```json
{"type":"sample","source":"simulated","sequence":1,"timestamp_ms":1792964337000,"ax":0.1,"ay":1.0,"az":9.81,"gx":2.0,"gy":15.0,"gz":0.5,"recorded_samples":0}
{"type":"status","recording":false,"source":"simulated","sample_rate_hz":50,"metadata":null,"recorded_samples":0,"file":null,"error":null}
{"type":"error","message":"A recording is already active. Stop it first."}
```

Recording is **global to this server**: all tabs see the same trial and any tab can
stop it. Closing a tab does not stop recording; reconnecting restores backend
status and active metadata. If the connection is lost, the browser explicitly
shows recording status as unknown. Stop explicitly before ending a session.
Server shutdown closes the CSV; a restart starts idle and preserves existing
files. Slow viewers may skip display samples; CSV recording occurs independently.

Use **one Uvicorn worker**. This foundation uses in-process state, no database or
cross-worker coordinator. `--reload` is useful during development but resets an
active recording on a code change. This is a local development app without
authentication; the documented command binds to your computer only.

## Hardware integration later

Replace `Acquisition.simulate()` with a serial/BLE/network adapter that produces
the same sample fields, then feeds the shared recording/broadcast path. Keep
sensor reading separate from the WebSocket request handler. Specify the real
sensor's units, sampling rate, timestamps, and dropout behavior before replacing
the simulator; the current nominal 50 Hz signal is for UI testing, not analysis.
Add EMG channels and their own sample-rate contract once that pipeline is known.

Reference/inspiration: [LonghornNeurotech/GUI](https://github.com/LonghornNeurotech/GUI)
provides a prior signal-viewing and training/deployment workflow. This project is
a new web implementation; it does not require or copy its Qt/hardware stack.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Automated integration tests check the served page/assets, streamed samples,
metadata errors, duplicate starts, CSV contents, reconnect behavior, and shared
state between viewers. Repeat the browser trial steps above after UI changes.
