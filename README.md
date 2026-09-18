# DRDO PXE — ADXL345 Real-Time Acceleration Acquisition GUI  `v3.0`

> **Organisation:** Defence Research & Development Organisation — Proof Experimental Establishment (PXE)  
> **Author:** Jaykishan  Das /jaykishandas30@gmail.com
  **Instagram:** @jay_dev._._
> **Hardware:** ADXL345 3-axis digital accelerometer → ESP32 / Arduino Nano 33 IoT → Ethernet UDP

---

### Commands For Terminal For Run The project
cd drdo_pxe_adxl_gui
>> py -3.11 -m venv venv311
>> Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
>> .\venv311\Scripts\activate
>> pip install -r requirements.txt
>> python main.py

## Table of Contents

1. [Overview](#1-overview)
2. [What's New in v3.0](#2-whats-new-in-v30)
3. [Project Structure](#3-project-structure)
4. [System Architecture](#4-system-architecture)
5. [Hardware Setup](#5-hardware-setup)
6. [UDP Packet Format](#6-udp-packet-format)
7. [Installation](#7-installation)
8. [Running the GUI](#8-running-the-gui)
9. [GUI Walkthrough](#9-gui-walkthrough)
10. [Plot Tabs — Detailed Reference](#10-plot-tabs--detailed-reference)
11. [CSV Output Format](#11-csv-output-format)
12. [Keyboard Shortcuts](#12-keyboard-shortcuts)
13. [Seismic Analyzer — Technical Reference](#13-seismic-analyzer--technical-reference)
14. [Arduino Firmware Reference](#14-arduino-firmware-reference)
15. [Troubleshooting](#15-troubleshooting)
16. [Version History](#16-version-history)

---

## 1. Overview

A real-time desktop data acquisition application built with **PyQt6** and **PyQtGraph** for monitoring the ADXL345 triaxial accelerometer over an Ethernet UDP link.

Key features at a glance:

- **Live acceleration plotting** — three EMA-smoothed curves (X/Y/Z) with per-axis visibility toggles and adjustable display offsets
- **Vibration event detection** — draggable threshold line, 7 ms debounce state machine, live event counter
- **Seismic event detector** — fully vectorised STA/LTA algorithm, real-time FFT power spectrum, event log table with draggable trigger/detrigger lines
- **CSV Viewer tab** — browse, load, and plot any previously saved ADXL345 CSV file without leaving the GUI
- **One-click CSV logging** — real-time streaming to disk with auto-generated timestamped filenames
- **Full session history** — up to 40 000 samples (≈ 6.7 minutes at 100 Hz) retained in circular ring buffers
- **LIVE / Full View modes** — LIVE scrolls the last 10 seconds; Full View auto-ranges to all collected data
- **Military-blue dark UI** — designed for field and laboratory environments

---

## 2. What's New in v3.0

| Version | Change |
|---------|--------|
| **v3.0** | Added **Seismic Analyzer tab** — vectorised STA/LTA detector, real-time FFT, event log table, draggable trigger/detrigger InfiniteLines, live dominant-frequency marker |
| **v3.0** | Fixed 7 bugs in `seismic_analyzer.py`: GUI-freezing Python loop (BUG-1), `FillBetweenItem` memory leak (BUG-2), `ScatterPlotItem.clear()` AttributeError (BUG-3), missing FFT fill (BUG-4), missing `abs()` on demeaned signal (BUG-5), `InfiniteLine.label` AttributeError (BUG-6), static method called as instance method for dominant frequency (BUG-7) |
| **v3.0** | Added **CSV Viewer tab** — browse any ADXL345 CSV, fuzzy column matching, encoding auto-detection, crosshair, per-curve visibility toggles, export PNG |
| **v2.8** | Time axis overhaul — elapsed time from acquisition start; LIVE = 10-second scrolling window; Full View auto-ranges to all data; buffer raised to 40 000 samples |
| **v2.7** | Z-axis displayed negated (below zero line); Vibration tab uses raw magnitude without EMA |
| **v2.2** | Per-axis display offset spinboxes in the Acceleration tab toolbar; `QKeySequence`/`QShortcut` moved to `QtGui` |
| **v2.0** | UDP transport replacing serial; dual-tab layout (Acceleration + Vibration) |

---

## 3. Project Structure

```
drdo_pxe_adxl_gui/
└── drdo_pxe_adxl_gui/
    ├── main.py               ← Application entry point; DRDOController wires UI ↔ data ↔ connection
    ├── data_handler.py       ← Thread-safe circular buffer; CSV logging; vibration state machine
    ├── connection_handler.py ← Owns UdpWorker; exposes connect_udp() / disconnect() / is_connected()
    ├── udp_worker.py         ← QThread: receives UDP packets, unpacks struct, calls DataHandler.ingest()
    ├── plot_manager.py       ← PlotManager (QTabWidget): AccelTab + VibrationTab + SeismicTab + CSVViewerTab
    ├── seismic_analyzer.py   ← SeismicAnalyzer (pure NumPy core) + SeismicTab (PyQt6 widget)
    ├── csv_viewer.py         ← CSVViewerTab: load & plot any ADXL345 CSV file on demand
    ├── ui_mainwindow.py      ← MainWindow: header bar, left sidebar, centre area, right stats panel
    ├── patch.py              ← Environment sanity checker (run before first launch)
    ├── requirements.txt      ← PyQt6, pyqtgraph, numpy, scipy
    ├── install.bat           ← Windows one-click installer
    ├── launch.bat            ← Windows one-click launcher
    └── resources/
        └── drdo_logo.png     ← Header bar and taskbar icon
```

### Module responsibilities

| Module | Role |
|--------|------|
| `main.py` | `DRDOController` — wires all signals; owns QTimers for plot refresh (80 ms / 12.5 Hz) and status updates (500 ms / 2 Hz) |
| `data_handler.py` | `DataHandler` — thread-safe `deque` ring buffers for `t, ax, ay, az, mag`; CSV writer; vibration state machine with 7 ms debounce |
| `connection_handler.py` | `ConnectionHandler` — thin facade over `UdpWorker`; directly forwards signals; exposes `connect_udp()` / `disconnect()` / `is_connected()` |
| `udp_worker.py` | `UdpWorker(QThread)` — `recvfrom(64)` loop; unpacks `struct{int16 x,y,z}` ÷ `SCALE_FACTOR (100.0)`; calls `DataHandler.ingest()` |
| `plot_manager.py` | `PlotManager(QTabWidget)` — owns all four tabs; single snapshot per tick shared across live tabs |
| `seismic_analyzer.py` | `SeismicAnalyzer` (NumPy STA/LTA + FFT processing) + `SeismicTab` (PyQt6 widget, three sub-panels) |
| `csv_viewer.py` | `CSVViewerTab` — static viewer; fuzzy column matching; four-encoding probe; crosshair; PNG export |
| `ui_mainwindow.py` | `MainWindow` — header, left sidebar (controls + action buttons), centre (plots injection point), right sidebar (live stats) |

---

## 4. System Architecture

```
┌───────────────────────────────────────────────────────────────────────┐
│  ADXL345 sensor → Arduino/ESP32 firmware → Ethernet UDP               │
└────────────────────────────────┬──────────────────────────────────────┘
                                 │  UDP port 8888 (configurable)
                                 ▼
┌───────────────── UdpWorker (QThread) ─────────────────────────────────┐
│  socket.recvfrom(64)  →  struct.unpack("<hhh")                         │
│  ÷ SCALE_FACTOR (100.0)  →  float m/s²                                │
│  e.g. raw int16 981 / 100.0 = 9.81 m/s²                               │
│  → SensorSample(timestamp, accel_x, accel_y, accel_z, magnitude)      │
│  → DataHandler.ingest()  [if ingestion gate is open]                   │
└────────────────────────────────┬──────────────────────────────────────┘
                                 │
┌───────────────── DataHandler ─────────────────────────────────────────┐
│  deque (t, ax, ay, az, mag) — MAX_BUFFER = 40 000 samples             │
│  AxisStats  (current / peak / mean)  per axis — incremental Welford   │
│  Vibration state machine  (rising-edge count with 7 ms debounce)      │
│  CSV writer  (streaming to disk at up to 133 Hz when logging active)  │
└──────────┬────────────────────────────────────────┬───────────────────┘
           │ get_snapshot()                         │ get_axis_stats()
           ▼                                        ▼
┌── PlotManager (QTabWidget) ──────┐   ┌── MainWindow ─────────────────┐
│  Tab 1 ⚡ AccelTab  (EMA X/Y/Z) │   │  Right sidebar:               │
│  Tab 2 📊 VibrationTab (raw mag) │   │    X / Y / Z current/peak/mean│
│  Tab 3 🌍 SeismicTab (STA/LTA)  │   │    Vibration event count + mag │
│  Tab 4 📂 CSVViewerTab (static)  │   │  Status bar (24 px)           │
└──────────────────────────────────┘   └───────────────────────────────┘
```

---

## 5. Hardware Setup

### ADXL345 Wiring (SPI, recommended for 100 Hz)

| ADXL345 Pin | Arduino / ESP32 |
|-------------|-----------------|
| VCC         | 3.3 V           |
| GND         | GND             |
| CS          | D10 (GPIO 5)    |
| SDO / MISO  | D12 (GPIO 19)   |
| SDA / MOSI  | D11 (GPIO 23)   |
| SCL / CLK   | D13 (GPIO 18)   |

### Network Setup

The firmware sends UDP packets to a **broadcast address** (e.g. `192.168.1.255`) or directly to the PC's IP.  
The GUI binds to `0.0.0.0:8888` (all interfaces) so no additional routing is required on a direct Ethernet link.

> **Tip:** Disable Wi-Fi on the PC and connect via a dedicated Ethernet cable or switch for the lowest-latency, lossless link. This eliminates packet-loss and jitter that wireless introduces.

---

## 6. UDP Packet Format

Each UDP datagram is exactly **6 bytes** — three signed 16-bit integers packed little-endian:

```
Offset  Type    Field   Description
──────  ──────  ──────  ────────────────────────────────────────────────────────
  0     int16   x_raw   X-axis acceleration × 100   (e.g.  981 = +9.81 m/s²)
  2     int16   y_raw   Y-axis acceleration × 100
  4     int16   z_raw   Z-axis acceleration × 100   (e.g. -350 = −3.50 m/s²)
```

**Python unpack:** `struct.unpack("<hhh", data)`

**Scale factor:** The firmware multiplies m/s² × 100 before packing into `int16`.  
`UdpWorker` divides by `SCALE_FACTOR = 100.0` to recover the original float:

```
Received int16 981  ÷ 100.0  →  float  9.81 m/s²
Received int16 -350 ÷ 100.0  →  float -3.50 m/s²
```

This approach avoids sending 12-byte float structs over UDP while preserving 2 decimal places of precision — sufficient for the ADXL345's ~0.004 m/s² per-LSB resolution.

**Magnitude:** `|a| = sqrt(ax² + ay² + az²)` is computed in `UdpWorker._parse_packet()` before the sample is passed to `DataHandler.ingest()`.

---

## 7. Installation

### Windows (one-click)

```batch
install.bat      ← runs:  pip install -r requirements.txt
launch.bat       ← runs:  python main.py
```

### Any platform (manual)

```bash
# 1. Create and activate a virtual environment (strongly recommended)
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows

# 2. Install all required dependencies
pip install -r requirements.txt

# 3. Verify the environment before launching
python patch.py

# 4. Launch the GUI
python main.py
```

### Dependencies

| Package     | Minimum Version | Purpose                                                            |
|-------------|-----------------|-------------------------------------------------------------------|
| `PyQt6`     | 6.7.0           | GUI framework — widgets, signals/slots, threading (`QThread`)     |
| `pyqtgraph` | 0.13.7          | Hardware-accelerated real-time plotting via OpenGL or QPainter    |
| `numpy`     | 1.26.0          | Vectorised signal processing (STA/LTA, FFT, EMA, statistics)     |
| `scipy`     | 1.12.0          | `scipy.signal.lfilter` — compiled C implementation of the EMA IIR filter in `plot_manager._ema()`. Required since v2.8.2 (BUG-EMA fix). |

> `pandas` is **not** required by the GUI. It can be installed separately for post-processing CSV files in Jupyter notebooks or standalone scripts.

---

## 8. Running the GUI

```bash
python main.py
```

**Recommended startup sequence:**

1. Power the ADXL345 / Arduino and confirm it is sending UDP packets.
2. Click **CONNECT UDP** in the left sidebar (default port 8888).
3. Confirm the LED turns **green** and the status bar shows `UDP listening on port 8888`.
4. Click **▶ START ACQUISITION** — plots begin updating and CSV logging starts (if the checkbox is enabled).
5. At the end of the test session click **■ STOP ACQUISITION**.
6. Use **⬇ SAVE DATA** to write a snapshot CSV at any time, or enable **Real-time CSV Logging** beforehand to capture every sample.

---

## 9. GUI Walkthrough

### Header Bar

| Element | Description |
|---------|-------------|
| DRDO Logo | Loaded from `resources/drdo_logo.png`; text fallback if missing |
| Organisation subtitle | Lab name and instrument version |
| LED indicator | Green = connected and socket bound; Red = offline |
| Clock | Live date/time, updated every 500 ms |
| User label | Mirrors the Operator name field in real time |

### Left Sidebar (290 px, scrollable)

| Section | Controls |
|---------|----------|
| **ETHERNET UDP — ADXL345** | Protocol display (read-only); UDP port spinbox (default 8888); Auto Reconnect checkbox; CONNECT UDP button; packet format hint |
| **OPERATOR** | Name field — embedded in auto-generated CSV filenames |
| **OUTPUT FILE** | Output directory picker; live filename preview |
| **ACQUISITION OPTIONS** | Enable Real-time CSV Logging checkbox; Pause Plot (keep logging) checkbox |
| **Action Buttons** | ▶ START, ■ STOP, ⬇ SAVE DATA, ✕ CLEAR ALL, DISCONNECT — pinned to sidebar bottom so they never scroll out of view |

### Centre — Plot Area

Full-height plot area containing four tabs (see Section 10 for full details on each).

### Right Sidebar (220 px, fixed)

| Section | Shows |
|---------|-------|
| **LIVE STATISTICS** | Current / Peak / Mean for axes X (blue), Y (green), Z (red) in m/s² |
| **VIBRATION MONITOR** | Detected vibration event count (large display); Min / Max / Mean of `|a|` magnitude |

### Status Bar (bottom, 24 px)

```
[Connection]  [Samples]  [Rate Hz]  [Data B/s]  [File]  [LOGGING]  [Vibrations]  [Message]
```

---

## 10. Plot Tabs — Detailed Reference

### Tab 1 — ⚡ Acceleration X / Y / Z

- Three overlapping **EMA-smoothed** curves: **X** (blue), **Y** (green), **Z** (red, negated for display)
- **Per-axis visibility checkboxes** toggle each curve independently without affecting data storage
- **Per-axis offset spinboxes** shift each displayed curve up/down without modifying raw data, CSV output, magnitude, or vibration counting
- **● LIVE mode** (default): X axis scrolls to show the last **10 seconds**; all 40 000-sample history is still in memory
- **⊕ Full View**: auto-ranges to fit all data from t = 0 to now
- **Manual pan/zoom** automatically exits LIVE mode; click **● LIVE** again to re-engage
- **Stat strip** (bottom, 24 px): MIN / MAX / MEAN / PEAK of the magnitude buffer
- **⬇ Export**: save current view as PNG

**Z-axis sign convention:**  
The raw ADXL345 with face-up reads ~+9.81 m/s² on Z (gravity). The display negates Z so it plots below zero, visually separating it from X and Y which hover near 0 g. Raw Z values in the CSV and vibration counter are always unmodified positive values.

### Tab 2 — 📊 Vibration Analysis

- Plots **raw, unsmoothed** magnitude `|a| = sqrt(ax² + ay² + az²)`
- **Why raw (no EMA)?** Smoothing would pull short-duration peaks below the threshold line, creating a confusing discrepancy between the displayed curve and the event counter. Raw data ensures the plot and counter are in perfect agreement.
- **Amber threshold line**: drag on-plot or type in the spinbox — changes apply immediately to both the display and `DataHandler`
- **Vibration count label**: updates every 500 ms; shows the session total from `DataHandler.vibration_count`
- **Debounce logic** (7 ms, in `DataHandler`): counts only rising edges; a burst that oscillates near the threshold is counted once, not hundreds of times
- **Threshold range**: 1.0 – 100.0 m/s² (default 12.0 m/s², deliberately above the ~9.81 m/s² gravity baseline)

### Tab 3 — 🌍 Seismic

Three resizable sub-panels stacked vertically:

| Sub-panel | Content |
|-----------|---------|
| **STA/LTA Ratio** | Gold ratio curve; red dashed **Trigger** line (draggable); amber dotted **Detrigger** line (draggable); red triangle markers at event onset times |
| **FFT Spectrum** | Blue filled-area power spectral density; gold dashed **Dominant Frequency** vertical marker; band-limited 0.5 – 45 Hz; Hann-windowed; last 5 seconds of signal |
| **Event Log** | Scrollable table: Time (s) / Peak Accel (m/s²) / Duration (s) / Peak Freq (Hz) / STA/LTA Peak |

**Parameter bar** (above all sub-panels):

| Control | Default | Effect |
|---------|---------|--------|
| STA (s) | 0.5 | Short-term average window duration |
| LTA (s) | 10.0 | Long-term average window duration |
| Trigger | 4.0 | STA/LTA ratio that marks event onset |
| Detrigger | 1.2 | STA/LTA ratio that marks event end |
| Clear Events | — | Wipes the event log and resets the state machine |

**Status footer** (below sub-panels): live Fs estimate · event count · dominant frequency · current STA/LTA · `● EVENT ACTIVE` indicator

### Tab 4 — 📂 CSV Viewer

A static data inspector for reviewing previously saved ADXL345 CSV files without leaving the GUI.

**Features:**
- **Browse** button opens a file dialog; path is shown in a read-only field
- **▶ View** button loads the CSV and renders all four channels (X, Y, Z, Magnitude)
- **Per-curve visibility checkboxes** (X / Y / Z / Mag) toggle each line independently
- **Z is negated** on display — consistent with Tab 1 (Acceleration)
- **Legend** in the plot corner identifies each colour
- **Crosshair** tracks the mouse with a live `(t, y)` coordinate readout
- **⊕ Full View** button auto-fits all loaded data
- **⬇ Export** saves the current plot view as PNG
- **Stats bar** (bottom): filename · sample count · duration · estimated Fs · peak magnitude
- **Fuzzy column matching**: case-insensitive, substring-based — handles headers like `Accel_X(m/s²)`, `accel_x`, `ax`, `AccelX`, etc.
- **Encoding auto-detection**: probes `utf-8-sig → utf-8 → cp1252 → latin-1`; `latin-1` never fails
- **Diagnostic errors**: if parsing fails, a detailed dialog shows detected headers, column mapping, and the first bad row

> **Note:** The CSV Viewer has no live refresh path. It is a static viewer only. The ● LIVE button is intentionally greyed out.

---

## 11. CSV Output Format

Files are written to the directory set in **OUTPUT FILE → Dir**.  
The filename is auto-generated as: `ADXL345_<UserName>_<YYYY-MM-DD_HH-MM-SS>.csv`

### Columns

```
Time_s, Timestamp, Accel_X(m/s²), Accel_Y(m/s²), Accel_Z(m/s²), Magnitude(m/s²)
```

| Column | Type | Description |
|--------|------|-------------|
| `Time_s` | float | Elapsed seconds from acquisition start (t = 0 at the first sample after START) |
| `Timestamp` | ISO-8601 string | Wall-clock datetime (`YYYY-MM-DD HH:MM:SS.ffffff`) |
| `Accel_X(m/s²)` | float | Raw X-axis acceleration — not EMA-smoothed; no display offset applied |
| `Accel_Y(m/s²)` | float | Raw Y-axis acceleration |
| `Accel_Z(m/s²)` | float | Raw Z-axis acceleration — **not negated** (positive = sensor face up) |
| `Magnitude(m/s²)` | float | `sqrt(ax² + ay² + az²)` — same value used by the vibration counter and seismic analyzer |

> **Important:** Display offsets and the Z-axis negation are **display-only**. The CSV always contains unmodified raw values from the sensor.

---

## 12. Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Space` | Toggle START ↔ STOP (whichever is currently enabled) |
| `Ctrl + S` | Save snapshot CSV |
| `Ctrl + L` | Clear all data (prompts for confirmation) |

---

## 13. Seismic Analyzer — Technical Reference

### Signal Preprocessing

1. **Axis selection** — configured by the `ANALYSIS_AXIS` constant in `seismic_analyzer.py` (default: `"mag"` — vector magnitude is most sensitive to omnidirectional events).
2. **DC removal (demeaning)** — subtracts the buffer mean.  
   Without this, the ~9.81 m/s² gravity component dominates the characteristic function (CF = signal²): CF_gravity ≈ 96 m²/s⁴ vs CF_blast ≈ 96 + small_change. The STA/LTA ratio barely moves. After demeaning, gravity is eliminated and a 1 m/s² blast gives CF ≈ 1.0, STA/LTA >> 1.

### STA/LTA Algorithm

```
CF[i]     = signal[i]²                 (characteristic function — energy proxy)
STA[i]    = mean(CF[i-nSTA : i])       (short-term energy average)
LTA[i]    = mean(CF[i-nLTA : i])       (long-term background energy)
ratio[i]  = STA[i] / LTA[i]
```

**Implementation**: fully vectorised with `numpy.cumsum` — O(N) in compiled C, runs in microseconds on 40 000 samples (BUG-1 fix: replaced a Python for-loop that took ~200 ms per refresh).

**Event state machine:**

```
ratio >= trigger_ratio  →  EVENT ON   (record onset time, begin tracking peak)
ratio <= detrig_ratio   →  EVENT OFF  (save SeismicEvent if duration ≥ 50 ms)
```

### FFT Spectrum

- Analyses the **last 5 seconds** of demeaned signal
- **Hann window** applied before FFT — eliminates spectral leakage from signal edge discontinuities
- Zero-padded to the next power of 2 for maximum FFT speed (Cooley-Tukey radix-2)
- One-sided PSD: non-DC, non-Nyquist bins multiplied by 2 for correct energy representation
- Band-limited to **0.5 – 45 Hz** (seismic band; safely below the 50 Hz Nyquist at 100 Hz sampling)
- **Dominant frequency** = `argmax(PSD)` within the band — stored as `SeismicAnalyzer.dom_freq`

### Sample-Count Fix

`SeismicAnalyzer` uses `DataHandler.sample_count` (an absolute counter that grows beyond the 40 000-sample buffer limit) rather than `snap["n"]` (which is pinned at `MAX_BUFFER` once the deque fills). Using `snap["n"]` would make the new-sample slice `range(40000, 40000)` = empty after the first 5 minutes — the detector would silently stop detecting events.

### Tuning Recommendations

| Scenario | Suggested Settings |
|----------|--------------------|
| Blast / detonation (sharp, broadband) | STA=0.2 s, LTA=10 s, Trigger=5.0, Detrigger=1.5 |
| Structural vibration (low-frequency, machinery) | STA=1.0 s, LTA=20 s, Trigger=3.0, Detrigger=1.2 |
| High-sensitivity (quiet environment, microseismic) | STA=0.5 s, LTA=10 s, Trigger=3.0, Detrigger=1.0 |
| Quick test / continuous signal | STA=0.3 s, LTA=5 s, Trigger=2.5, Detrigger=0.8 |

---

## 14. Arduino Firmware Reference

### Packet Format the Worker Expects

`udp_worker.py` unpacks **3 × signed int16, little-endian** and divides by `100.0`.  
The firmware must therefore send acceleration in **m/s² × 100** (integer fixed-point arithmetic):

```
Accel = +9.81 m/s²  →  send int16  +981
Accel = -3.50 m/s²  →  send int16  -350
Accel =  0.00 m/s²  →  send int16     0
```

### Minimal ESP32 UDP Sender

```cpp
#include <WiFi.h>
#include <WiFiUdp.h>
#include <SPI.h>
#include <ADXL345_SPI.h>   // or any ADXL345 library that returns float m/s²

const char* PC_IP      = "192.168.1.100";  // Replace with your PC's IP address
const int   UDP_PORT   = 8888;
// Multiply m/s² by 100 before packing into int16.
// UdpWorker divides by 100.0 on receive to recover the original float.
const float SCALE_TO_INT = 100.0f;

WiFiUDP  udp;
ADXL345  adxl;

void setup() {
    WiFi.begin("SSID", "PASSWORD");
    while (WiFi.status() != WL_CONNECTED) delay(500);
    adxl.begin();
    adxl.setRange(ADXL345_RANGE_2_G);
    adxl.setDataRate(ADXL345_DATARATE_100_HZ);   // 100 Hz → 10 ms loop delay
}

void loop() {
    float ax, ay, az;
    adxl.readAcceleration(ax, ay, az);   // library returns float m/s²

    // Fixed-point encoding: 9.81 m/s² × 100 = 981 → packed as int16
    int16_t buf[3] = {
        (int16_t)(ax * SCALE_TO_INT),
        (int16_t)(ay * SCALE_TO_INT),
        (int16_t)(az * SCALE_TO_INT),
    };

    udp.beginPacket(PC_IP, UDP_PORT);
    udp.write((uint8_t*)buf, 6);   // exactly 6 bytes: struct { int16 x, y, z }
    udp.endPacket();

    delay(10);   // 100 Hz sample rate
}
```

### Ethernet (W5500) Version

Replace `WiFiUDP` with `EthernetUDP` from the Arduino `Ethernet` library.  
The 6-byte packet format and port number are identical — the PC-side code requires no changes.

---

## 15. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| LED stays red after CONNECT | Wrong port or OS firewall blocking UDP 8888 | Check `spin_udp_port` matches firmware; add Windows Firewall inbound rule for UDP port 8888 |
| LED green but plots don't start | Acquisition gate not opened | Confirm you clicked **▶ START** after CONNECT |
| All three axes show ~9.81 m/s² | Normal — gravity is always present when sensor is flat | Expected at rest; gravity is removed in the Seismic tab via demeaning |
| Z axis reads ~−9.81 on display | Z is negated for display (display convention only) | Intentional; raw CSV stores the positive value |
| Vibration count increments too fast | Threshold too low (below gravity + noise floor) | Raise the amber threshold line in Tab 2 above ~10 m/s² |
| Vibration count stays at 0 | Threshold too high | Lower the threshold line or reduce the spinbox value |
| STA/LTA never exceeds trigger | LTA window too long or trigger ratio too high | Reduce LTA (s) to 5, or lower Trigger ratio to 2.5 |
| `● EVENT ACTIVE` never turns off | Detrigger ratio never reached | Lower the Detrigger ratio (e.g. from 1.2 to 0.8) |
| `● EVENT ACTIVE` permanently stuck | Detrigger was set >= Trigger | The spinboxes enforce the invariant; re-open the tab to reset |
| FFT shows no clear peak | Signal amplitude below noise floor | Reduce ADXL345 range to ±2 g (highest sensitivity) |
| CSV Viewer shows "Required columns not found" | CSV header format doesn't match | The file must contain columns with `time_s`, `accel_x`, `accel_y`, `accel_z` (case-insensitive substrings) |
| GUI is sluggish | Large dataset + slow GPU | Reduce `LIVE_WINDOW_S` in `plot_manager.py` (default 10 s) |
| `ImportError: No module named 'scipy'` | scipy not installed (required since v2.8.2) | Run `pip install -r requirements.txt` |
| `ImportError: No module named 'PyQt6'` | Dependencies not installed | Run `pip install -r requirements.txt` or double-click `install.bat` |
| CSV file is empty after STOP | Logging checkbox was unchecked, or output folder is read-only | Enable **Enable Real-time CSV Logging** before START; verify folder permissions |
| Seismic event table is empty | Signal demeaned to near-zero (sensor at rest) | Trigger a tap or vibration; lower the Trigger ratio if near-threshold events are expected |

---

## 16. Version History

| Version | Date | Key Changes |
|---------|------|-------------|
| **v3.0.2** | 2026-06 | Fixed misleading bug-number labels in `seismic_analyzer.py` comments (SAMPLE-COUNT fix mislabelled BUG-1; RATIO-SYNC fixes mislabelled BUG-2); fixed redundant `self._filepath` assignment in `csv_viewer._on_view()`; fixed `plot_manager.refresh()` comment referencing removed `VIB_BUF` constant; fixed `_ema()` docstring version tag (was v2.8.3, is v2.8.4); removed dead `DataHandler` import from `seismic_analyzer.py`; added humanized inline comments throughout all modules |
| **v3.0.1** | 2026-06 | Removed unused `connected` attribute from `ConnectionHandler`; removed unused `C_BORDER_LIT` constant from `ui_mainwindow.py`; corrected LIVE mode docstring (was "fixed 0–300 s axis"); added `setMenuEnabled`, `setClipToView`, `setDownsampling` to Seismic sub-panels |
| **v3.0** | 2026-06 | Seismic Analyzer tab: vectorised STA/LTA, real-time FFT, event log table, draggable trigger/detrigger lines; CSV Viewer tab: browse and plot any saved CSV; 7 bug fixes in `seismic_analyzer.py` (BUG-1 through BUG-7) |
| **v2.8.4** | 2026-06 | BUG-EMA-INIT fix: warm-start `lfilter` with `lfilter_zi` to eliminate ramp-from-zero on AccelTab; BUG-LABEL fix: removed `InfiniteLine.label.setFormat()` call; BUG-DRAG fix: clamped VibrationTab threshold drag to spinbox bounds |
| **v2.8.2** | 2026-06 | BUG-EMA fix: replaced 40 000-iteration Python loop in `_ema()` with `scipy.signal.lfilter` — `scipy` added as required dependency |
| **v2.8.0** | 2026-06 | Time axis overhaul: elapsed time from `start_time`; LIVE = 10-second scrolling window; Full View; `MAX_BUFFER` raised to 40 000; `VIB_BUF` constant removed (all tabs use `FULL_BUF`) |
| **v2.7.1** | 2026-06 | Z-axis display negated in AccelTab; `DataHandler.get_axis_stats()` negates Z current/mean for sidebar display |
| **v2.7.0** | 2026-06 | VibrationTab switched to raw (un-smoothed) magnitude |
| **v2.2.2** | 2026-05 | BUG-SHADOW fix: `AxisStatBox.update()` and `VibrationStatsBox.update()` renamed to `set_stats()` to stop shadowing `QWidget.update()` |
| **v2.2.1** | 2026-05 | Removed unused `C_BORDER_LIT` colour constant |
| **v2.2.0** | 2026-05 | Per-axis display offsets (spinboxes in AccelTab toolbar); `QKeySequence`/`QShortcut` moved from `QtCore` → `QtGui` |
| **v2.0** | 2026-05 | UDP transport replacing serial; dual-tab layout (Acceleration + Vibration) |
| **v1.0** | 2026-05 | Initial release — MPU6050, serial UART, single Acceleration tab |

---

*DRDO PXE — ADXL345 DAQ System v3.0 — Internal documentation*
