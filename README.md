# Longhorn Neurotech EMG collection

A FastAPI/WebSocket app for collecting and reviewing labeled EMG trials. Use the
built-in simulator or the experimental Mudra Link Bluetooth bridge. Each trial
has a raw EMG CSV and a JSON metadata/quality report. Approved trials can be
exported to a local folder or a GitHub repository.

The Mudra connection and signal-quality criteria have **not been tested with
physical hardware in this app**. Validate the band, sampling rate, timestamps,
and recording completeness with the team before collecting participant data.

## Start the app on Windows

Open PowerShell in the repository folder. The first time, create the Python 3.10+
environment and install the app:

    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt

For later runs, start the server with:

    .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Leave PowerShell open while
using the app; Ctrl+C stops the server. If port 8000 is in use, replace it with
8001 in the command and browser address. On macOS/Linux, create an environment
using python3 -m venv .venv, then use .venv/bin/python for the commands.

## Record a trial

1. Select **Simulated EMG** or **Mudra Link EMG** as the signal source.
2. Enter sampling frequency, session ID, participant ID, gesture, and the save
   destination. The app cannot configure the sensor's sampling rate. Leave the
   frequency blank if it is unknown; frequency plots require a rate.
3. Click **Run**. The app counts down for three seconds, then starts recording.
   Click **Stop** to finish; there is no fixed duration. A unique trial ID is
   generated automatically.
4. Review the raw-data checks. **Accept** exports the trial; **Reject / repeat**
   retains it locally as a rejected attempt. You can inspect trial files under
   **Data View**.

There are four named active gestures, two explicitly marked placeholder choices,
and idle. Confirm the last two names with the team before using those labels for
data collection.

The simulator's 1,000 Hz EMG is synthetic. Its filters and quality thresholds
are demonstration settings, not validated settings for a physical sensor. Graph
settings do not modify the saved raw samples.

## Live signal display

The waveform and frequency charts use the locally bundled uPlot 1.6.32
(MIT license); no browser extension or internet connection is needed. The
waveform updates on animation frames, with a target of up to 30 redraws per
second; the FFT updates at most four times per second. Actual performance
depends on the computer and browser. Pause the plots to inspect values or drag
to zoom, then use Reset zoom or Resume plots. Pausing affects the display only;
the backend continues recording.

The waveform uses seconds relative to the latest sample when a rate is known
and sample offsets when it is unknown. The 512-point Hann-windowed FFT reports
frequency in Hz and magnitude in the input unit, not power spectral density.
Entered Mudra rates are unverified: their time and frequency axes are estimates.
Dense waveforms are visually downsampled while preserving local peaks; the FFT
and saved CSV use the full-resolution samples.

Simulated EMG is shown in arbitrary units. Mudra EMG is shown in raw signed
counts by default because its voltage calibration is not established. A
verified, common µV-per-count factor may optionally be entered for display
only. This never changes the recorded counts or the quality checks.

## Connect a Mudra Link band

The optional bridge uses Bluetooth on the **same computer running the backend**.
It forwards the band's FFF4 notifications to the app. The protocol is based on
the supplied capture scripts and has not been verified with a physical band in
this application.

1. Install the optional dependencies in the project environment:

       .\.venv\Scripts\python.exe -m pip install -r requirements-mudra.txt

2. Turn on Bluetooth, wake the band, and find its address:

       .\.venv\Scripts\python.exe tools\mudra_bridge.py --scan

3. Start the app server. In a **second terminal**, replace YOUR_BAND_ADDRESS
   with the address from the scan and start the bridge:

       .\.venv\Scripts\python.exe tools\mudra_bridge.py --address YOUR_BAND_ADDRESS

   If the app server is on port 8001, append
   --url ws://127.0.0.1:8001/ws/mudra. Accept any operating-system pairing
   prompt. Close other apps using the band. If activation is required, use the
   official Mudra app, close it, and retry. This bridge does not read credentials
   or implement a license handshake.
4. In the browser select **Mudra Link EMG** and wait for **Mudra: connected ·
   receiving**. Enter the sampling frequency only if known; it labels the data
   and controls the display, but does not change the band's rate.
5. Click **Run**, perform the gesture after the countdown, and press **Stop**.
   Review the EMG checks before accepting or rejecting the trial.

The bridge currently accepts only the supplied script's 112-byte layout: 18
samples of three little-endian signed 16-bit values followed by a 32-bit device
timestamp. An unexpected size stops the bridge; it is not silently discarded.
The three channels are saved as raw counts, not calibrated voltage. The host
receipt timestamp is repeated for each sample in a frame, and the original
per-frame device timestamp and sample index are retained.

The supplied scripts mention different possible sample rates. The app does not
assume one. An optional --sample-rate argument can advertise a known rate from
the bridge; a rate entered in the form must agree with it. Both are treated as
user-supplied and unverified. When the rate is unknown, the waveform uses sample
indices and the spectrum is unavailable. Physical-sensor filters are disabled
until the device settings have been validated.

Bridge-generated frame counters can detect gaps within the app connection but
cannot detect Bluetooth packets lost before the bridge receives them. Queue
overflow, unexpected frame sizes, disconnects and stream timeouts invalidate an
active trial. Recording boundaries are timed by the backend and may lag device
receipt; lossless capture and timestamp alignment are not established.

## Save destinations

- **Local folder:** Enter a path on the backend computer. Leave it blank to use
  recordings/approved/. Existing files with different content are not overwritten.
- **GitHub repository:** The form defaults to LonghornNeurotech/EMG-Data-Base,
  branch main, folder recordings. These values can be changed; for example,
  choose raw-data to match the database repo's suggested layout. The branch
  must exist. Install [GitHub CLI](https://cli.github.com/) on the backend
  computer and run gh auth login with an account that can write to the repo.
  Accepting a trial creates a commit with the CSV and metadata. Review repository
  visibility and participant-data policy before uploading.

The backend also retains a staging copy in recordings/ (set EMG_DATA_DIR to
change it). Failed exports remain in review for retry. Recordings are excluded
from this app's own Git repository by .gitignore.

## Review and limitations

The review panel checks the saved CSV for the expected row count, valid values,
timestamps and locally generated sample sequence. It displays per-channel
standard deviation, RMS and range. For Mudra, minimum-length and flatline
thresholds are **not assessed** until the team defines device-specific values;
accepting such a trial requires a written reason. Clipping, electrode contact,
gesture correctness, actual sampling rate, and missing Bluetooth notifications
are not established by these checks. Corrupt or interrupted recordings cannot
be accepted.

The backend is unauthenticated and unencrypted; use it only on localhost or a
trusted isolated network. Browser disconnects do not stop the backend trial.
Use a single Uvicorn worker.

## Tests

    .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
    .\.venv\Scripts\python.exe -m pytest -q
    node tests/test_signal.js
    node tests/test_charts.js
    node tests/test_ui.js

The Mudra tests use synthetic frames and mocked Bluetooth. A successful test
does not verify the physical band, license state, firmware, sampling rate,
signal quality, or sustained acquisition.
