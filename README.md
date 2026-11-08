# IMU data collection and prediction web app

A laptop-hosted FastAPI/WebSocket prototype for labeled IMU trials. It can run
with built-in simulated IMU/EMG or receive a device stream (e.g. a Raspberry Pi)
through a **proposed** WebSocket protocol. Four active forearm gestures and idle
follow the semester implementation plan. The device protocol, sensor settings and
validation criteria must be confirmed with the IMU/inference team before real
participant collection. The Raspberry Pi hardware, sensor driver, and model have
not yet been verified with this application.

## Run the application (Windows)

Clone this repository, or open your existing copy in VS Code and run `git pull`.
From the repository folder, create a Python 3.10+ environment once:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start the server (reuse the existing environment on later runs):

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. If port 8000 is in use, replace `8000` with `8001`
in the command and URL. Stop the server with Ctrl+C. On macOS/Linux use
`python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements.txt`,
and `.venv/bin/python -m uvicorn app.main:app`.

## Try collection without hardware

1. In the browser choose **Simulated IMU + EMG**. The Pi status should still say
   disconnected; browser connection and device connection are different things.
2. Enter participant and session IDs, choose **forearm raise, forearm lower,
   pronation, supination, or idle**, and choose a trial number. Optional notes,
   mounting location, orientation, and sensor ranges are available.
3. Start the countdown, perform the trial when recording starts, and stop it.
   The status, elapsed time, and sample count update in the browser.
4. Accept or reject the trial. Rejected recordings remain on disk for audit but
   are not valid training examples. To repeat, use the same fields and start a
   new attempt. Generated filenames are unique and never overwrite each other.
5. Files are stored on the **server laptop** in `recordings/`, not downloaded in
   the browser. Each attempt has an IMU CSV and a JSON sidecar. The built-in
   simulated source also produces a separate simulated EMG CSV. The app does
   not upload participant data to Git; `recordings/` is ignored.

The default simulator shows an **explicitly simulated** idle prediction with
example confidence, not an ML model. On the Pi source, only messages from the
connected device appear as predictions; stale predictions disappear after about
three seconds. Confidence is the device's reported value, not a calibrated
probability guarantee. Browser plots and EMG filters are display-only; recordings
contain raw samples. Actual EMG acquisition is not implemented.

## Simulate the Pi connection

Keep the server running and open a second terminal in the repository folder:

```powershell
.\.venv\Scripts\python.exe tools\simulated_pi.py --url ws://127.0.0.1:8000/ws/device
```

If your server uses port 8001, change the URL to port 8001. In the browser the
Pi indicator should say **connected / receiving**. Select **Raspberry Pi IMU**.
The device ID and sensor model identify this client as **simulated**, even
though it uses the same protocol intended for a Pi. Try a trial and observe the
start/stop commands printed by the client. Ctrl+C stops the simulated client.

For an actual Pi on the same trusted local network, the server must bind to the
laptop's network interface, e.g. `--host 0.0.0.0`, and the producer would connect
to `ws://LAPTOP_IP:8000/ws/device`. This prototype has **no authentication or
transport encryption**, so use only a trusted isolated network; do not expose it
to the public internet. Keep participant IDs pseudonymous and follow your team's
storage practices. Browsers can connect to the laptop using its LAN address.

## Provisional device protocol

Only one device WebSocket is supported. The sensor process connects to
`/ws/device` and sends this first:

```json
{"type":"hello","device_id":"pi-01","sensor_model":"LSM6DSO32","sample_rate_hz":100,"acceleration_unit":"m/s^2","angular_velocity_unit":"rad/s","timestamp_clock":"device monotonic"}
```

The backend responds with `hello_ack`. The device sends sample messages:

```json
{"type":"sample","timestamp_ns":1000000000,"sequence_number":0,"ax":0.1,"ay":0.2,"az":9.81,"gx":0.01,"gy":0.02,"gz":0.03}
```

`timestamp_ns` and `sequence_number` are nonnegative **JSON integers** from the
producer; document the actual clock domain. The backend preserves them in the
CSV. A monotonically increasing sequence counter **must increment by one across
the device stream**, including between trials. A gap, duplicate, out-of-order
sample, malformed message, device disconnect, or ~3 second sample timeout
interrupts an active Pi trial and marks its sidecar `invalid`. It cannot be
accepted in the UI. For batching use:

```json
{"type":"sample_batch","samples":[{"timestamp_ns":1000000000,"sequence_number":0,"ax":0.1,"ay":0.2,"az":9.81,"gx":0.01,"gy":0.02,"gz":0.03}]}
```

Batches contain 1–500 samples; each entry omits `type`. All entries are
validated before processing. `timestamp_ns` may be either Unix or monotonic time
as declared in `hello`—do not assume two devices share a clock.

The producer can send inference or errors:

```json
{"type":"prediction","gesture":"pronation","confidence":0.82}
{"type":"error","message":"Sensor read failed"}
```

Predictions must use one of the five labels and confidence in [0,1]. Invalid
messages receive an error response. During a trial invalid messages mark the
trial invalid. A device that loses its socket is reported as disconnected.
When recording begins/ends the backend sends the device
`{"type":"start_recording","metadata":{...}}` and
`{"type":"stop_recording"}`. The device is expected to stream continuously;
these commands are notifications and **are not acknowledged or verified yet**.
The Pi client must implement its own acquisition queue and handle backpressure.
The prototype does not yet guarantee lossless operation under load.

The browser uses `/ws` with `set_source`, `start_recording`, `stop_recording`,
`review_trial`, and `get_status` messages. `GET /health` gives current state.
The backend records device data before publishing it to browser queues; slow
browser viewers can skip samples without intentionally dropping recorded data.
A sample rate reported in the hello is nominal; it is not measured by the app.

## Files and trial validity

IMU CSV columns are
`participant_id,session_id,gesture,trial_id,sensor_model,timestamp_ns,sequence_number,ax,ay,az,gx,gy,gz`.
Pi records preserve the producer's counter and timestamps. Built-in simulator
records use a trial-local sequence starting at 0 and host Unix nanoseconds.
Acceleration is m/s² (including gravity) and angular velocity is rad/s.

The JSON sidecar records metadata, nominal sample rate, clock description,
units, source, sample counts, and `review_status` (`recording`, `needs_review`,
`accepted`, `rejected`, or `invalid`). Train only on **accepted** recordings;
never treat missing/unreadable sidecars as accepted. Pi recordings are IMU-only.
Built-in simulated recordings include a companion `-emg.csv` with four
arbitrary-unit synthetic channels, not calibrated voltage. EMG timestamps are
synthetic host-anchored batches and are not synchronized sensor timestamps.

Stopping a trial normally leaves it `needs_review`. Pi disconnects, malformed
messages, sequence gaps, timeouts, or write failures mark an active trial
`invalid`. A normal server shutdown while recording also marks it invalid. On
restart, sidecars left in `recording` state are marked invalid where writable;
review any unreadable metadata manually. Review state itself is not restored in
the UI after restart. Browser disconnects alone do not stop a backend trial.

Use one Uvicorn worker; state is in memory. Switching sources or starting a new
trial is blocked until the current trial is stopped and reviewed. A new attempt
gets a unique filename even if its labels match an earlier trial; operators must
track trial IDs and acceptance decisions. Set `IMU_DATA_DIR` to change the
output folder. Logs/recordings remain on the backend machine.

## Signal display and limitations

The six IMU channels are acceleration X/Y/Z and gyroscope X/Y/Z. Axis selection,
vertical scales and time window affect display. The time-window plot currently
uses the reported nominal rate and a fixed sample buffer; it is a visualization,
not a precise measurement of irregular arrival timing. Built-in simulation also
has 1–4 selectable EMG channels, a 20–200 Hz bandpass, a 60 Hz notch, and a
256-sample Hann-windowed spectrum; the filters and spectrum use a nominal 1 kHz
synthetic stream. The Pi source hides the simulated EMG panel. These tools are
not validated processing for real sensors or clinical use.

## Verification and outstanding integration

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
node tests/test_signal.js
```

Tests cover ten sequential simulated recordings, CSV metadata, no overwritten
filenames, Pi sample/prediction messages, disconnects, sequence gaps, malformed
messages, invalid trials, and interrupted-sidecar recovery. Node is only needed
for the optional browser signal-math test. Tests are simulation-based; they do
not demonstrate real Pi/LSM6DSO32 integration, model accuracy, or the plan's
10-minute/<1% dropped-sample criterion. File browsing/export, prediction
staleness requirements, command acknowledgments, hardware queue sizing,
sampling-rate verification and exact protocol remain work with the other team.

| Failure mode | Current response / limitation |
| --- | --- |
| Pi disconnect, sample gap, or timeout during recording | Trial marked invalid; device status shown; reconnect and repeat. |
| Malformed Pi message | Error returned; an active Pi trial is marked invalid. |
| Browser disconnect | Backend continues; reconnect to see in-memory trial status. |
| Crash or file-write failure | Sidecar invalidated on recovery where possible; never assume missing sidecars are valid. |
| Duplicate labels | Unique filenames avoid overwrite, but the operator must manage trial numbers. |
| Wrong sensor units, timestamp clock, sample rate, or physical placement | Not automatically detectable; confirm the device contract and pilot recordings. |
| Slow disk/network at acquisition rate | Lossless acquisition not established; validate with the real Pi and sequence checks. |
