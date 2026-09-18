"""
DRDO PXE — ADXL345 Data Acquisition System
data_handler.py  |  v2.8.1

WHAT THIS FILE DOES
-------------------
Owns ALL sensor data for the current acquisition session.
- Receives raw SensorSample objects from UdpWorker (via ingest())
- Maintains circular ring buffers for time, X/Y/Z acceleration, and magnitude
- Tracks running per-axis statistics (current, peak, mean)
- Counts vibration events using a threshold-crossing state machine with debounce
- Writes data to a CSV file in real time when logging is active
- Provides thread-safe snapshots to the plot tabs

WHY A SEPARATE CLASS (not in main.py)?
---------------------------------------
DataHandler is the single source of truth for all sensor data. By isolating
it here, both the background UDP thread (UdpWorker) and the GUI main thread
(plot timers, status timers) can access data safely. Everything is guarded
by a re-entrant lock (_lock) so no data gets corrupted by concurrent access.

THREAD SAFETY
-------------
Every public method acquires self._lock (an RLock — re-entrant so the same
thread can lock it twice, e.g. if stop_logging() is called inside clear()).
UdpWorker runs ingest() from the background thread; plot timers call
get_snapshot() from the GUI thread. Without the lock they would race.

INGESTION GATE
--------------
_ingesting is False by default. Packets arrive continuously once CONNECT is
clicked, but they are silently dropped until START is clicked. This lets the
operator check that the sensor is streaming (LED goes green) before beginning
a logged session at a known time (t=0 anchored to the very first accepted sample).

CHANGELOG
---------
v2.8.1 — BUG-LOGFILE fix: start_logging() now defensively closes any
          previously open file handle before opening a new one. Under normal
          flow (stop_logging precedes start_logging) this guard is a no-op.
v2.8.0 — MAX_BUFFER raised to 40 000; _start_time anchors t=0 to first
          ingested sample; get_snapshot() includes start_time; CSV gains
          Time_s column; clear() resets _start_time.
v2.7.1 — get_axis_stats() negates Z current/mean for display convention.
"""

from __future__ import annotations

import csv
import threading
from collections import deque
from datetime import datetime

import numpy as np

# ── Buffer and algorithm constants ────────────────────────────────────────────

# 300 s × ~133 Hz = ~40 000 samples — enough for a full Arduino session without
# wrapping. Raising this uses more RAM but keeps full history available for
# Full View and the Seismic tab's FFT analysis.

MAX_BUFFER = 40_000

# Write to the CSV in bursts of 50 rows, then flush. This balances two risks:
#   - Flushing every row: lots of small OS write calls, hurts performance.
#   - Never flushing: power loss could lose minutes of data.

CSV_FLUSH_N = 50

# Vibration threshold: above 12.0 m/s² means something exceeded 1.22 g.
# At rest the ADXL345 reads ~9.81 m/s² due to gravity, so 12.0 m/s² means
# the sensor has detected ~0.22 g above gravity — a clear real event.

DEFAULT_THRESHOLD = 12.0   # m/s²

# Minimum time (seconds) between two consecutive counted vibration events.
# Prevents a signal that briefly oscillates near the threshold from being
# counted many times in a single burst. 7 ms = ~0.7 samples at 100 Hz.

VIB_DEBOUNCE_S = 0.007

# ── Sensor sample dataclass ───────────────────────────────────────────────────

class SensorSample:
    
    """
    Immutable record for one ADXL345 reading.

    __slots__ is used instead of a regular dict-backed class because we create
    one SensorSample per incoming UDP packet (~133/s). __slots__ eliminates the
    per-instance __dict__, saving ~200 bytes per object and reducing GC pressure.

    magnitude is computed once here and stored so neither the plot tab nor the
    vibration counter needs to recompute sqrt(ax²+ay²+az²) independently.
    """
    __slots__ = ("timestamp", "accel_x", "accel_y", "accel_z", "magnitude")

    def __init__(self, ts: float, ax: float, ay: float, az: float):
        self.timestamp = ts        # Unix epoch seconds (time.time())
        self.accel_x   = ax        # m/s²
        self.accel_y   = ay        # m/s²
        self.accel_z   = az        # m/s²
        self.magnitude = float(np.sqrt(ax*ax + ay*ay + az*az))   # |a| m/s²


# ── Per-axis running statistics ───────────────────────────────────────────────

class AxisStats:
    
    """
    Tracks current, peak, and mean values for a single axis.

    WHY INCREMENTAL MEAN (Welford's):
    Rather than re-summing the whole buffer every 500 ms, we maintain a
    running mean using the one-pass incremental formula:
        mean_new = mean_old + (value - mean_old) / n
    This is O(1) per sample instead of O(n) and never has numerical overflow.

    __slots__ saves memory — three of these exist simultaneously (X, Y, Z).
    """
    __slots__ = ("current", "peak", "mean", "_n")

    def __init__(self):
        self.reset()

    def reset(self):
        
        """Called on CLEAR — wipes all statistics for a fresh session."""
        
        self.current = 0.0
        self.peak    = 0.0
        self.mean    = 0.0
        self._n      = 0

    def update(self, v: float):
        
        """
        Incorporate one new sample.

        peak stores the maximum *absolute* value seen (always positive).
        current and mean are signed so the sidebar can distinguish e.g.
        +9.81 (Z up) from −9.81 (Z down).
        """
        self.current = v
        if abs(v) > self.peak:
            self.peak = abs(v)
        self._n  += 1
        # Welford's incremental mean — numerically stable, no accumulation error
        self.mean += (v - self.mean) / self._n

    def as_dict(self) -> dict:
        
        """Return a plain dict for the UI sidebar update calls."""
        
        return {"current": self.current, "peak": self.peak, "mean": self.mean}


# ── Main data handler ─────────────────────────────────────────────────────────

class DataHandler:
    
    """
    Thread-safe data store for an ADXL345 acquisition session.

    One shared instance is created in DRDOController.__init__() and passed to
    both ConnectionHandler (→ UdpWorker) and PlotManager. The UDP background
    thread calls ingest() at ~133 Hz; the GUI timers call get_snapshot() at
    12.5 Hz. The RLock ensures these never interleave mid-write.
    """

    def __init__(self):
        
        # RLock (re-entrant) rather than Lock because clear() calls
        # stop_logging() which also acquires the lock — a plain Lock would
        # deadlock on the second acquire from the same thread.
        
        self._lock = threading.RLock()

        # ── Circular ring buffers ─────────────────────────────────────────
        
        # deque(maxlen=N) automatically drops the oldest item when full,
        # giving us a rolling 300-second window with zero manual index arithmetic.
        # We keep five separate deques (one per channel) rather than a deque of
        # tuples so numpy can convert each column directly without a zip/unzip.
        
        self.timestamps = deque(maxlen=MAX_BUFFER)
        self.accel_x    = deque(maxlen=MAX_BUFFER)
        self.accel_y    = deque(maxlen=MAX_BUFFER)
        self.accel_z    = deque(maxlen=MAX_BUFFER)
        self.magnitude  = deque(maxlen=MAX_BUFFER)

        # ── Per-axis statistics ───────────────────────────────────────────
        
        self.stats_x = AxisStats()
        self.stats_y = AxisStats()
        self.stats_z = AxisStats()

        # ── Vibration state machine ───────────────────────────────────────
        
        self.vibration_threshold = DEFAULT_THRESHOLD   # m/s²
        self.vibration_count     = 0
        self._above_threshold    = False   # True while magnitude ≥ threshold
        
        # Unix timestamp of the last counted vibration crossing.
        # Used to enforce the 7 ms debounce window.
        
        self._last_vib_time      = 0.0

        # ── Magnitude running summary (for Vibration sidebar box) ─────────
        
        self._mag_min  = float("inf")
        self._mag_max  = float("-inf")
        self._mag_mean = 0.0
        self._mag_n    = 0

        # ── Ingestion gate ────────────────────────────────────────────────
        
        # False by default. Only set to True when the user clicks START.
        # Packets that arrive before START are visible to the byte-rate counter
        # but are not stored in the buffers or written to CSV.
        
        self._ingesting = False

        # ── Session time anchor ───────────────────────────────────────────
        
        # Records the Unix timestamp of the VERY FIRST ingested sample so
        # that t=0 on the plot is always the moment START was clicked,
        # regardless of when in the session the plot is viewed.
        
        self._start_time   = 0.0
        self._first_sample = True   # becomes False after the first ingest()

        # ── CSV state ─────────────────────────────────────────────────────
        
        self._csv_file    = None      # open file handle when logging is active
        self._csv_writer  = None      # csv.writer wrapping _csv_file
        self._csv_path    = ""        # path of the currently open CSV
        self._flush_ctr   = 0         # counts rows since the last flush()
        self.logging_active = False   # public flag so main.py can read it

        # Total sample count — never wraps at MAX_BUFFER. Used by SeismicAnalyzer
        # to detect truly new samples even after the circular buffer is full
        # (at that point snap["n"] stays pinned at 40 000 forever, but
        # sample_count keeps growing, so the difference gives the correct
        # number of new samples to scan each tick).
        
        self.sample_count = 0

    # ── Gate control ──────────────────────────────────────────────────────────

    def set_ingesting(self, value: bool):
        
        """
        Open or close the ingestion gate.

        Called by DRDOController:
          START → set_ingesting(True)   — data starts flowing into buffers
          STOP  → set_ingesting(False)  — data is no longer stored (but still
                                          counted for the byte-rate display)
        """
        with self._lock:
            self._ingesting = value

    # ── Ingest ────────────────────────────────────────────────────────────────

    def ingest(self, sample: SensorSample):
        
        """
        Store one sensor sample. Called from the UdpWorker background thread at ~133 Hz.

        The gate check is first so rejected packets cost almost nothing.
        Everything inside the lock is O(1) — no sorting, no searching, no
        re-computing full-buffer statistics. This keeps the background thread
        lean so the GUI stays responsive.
        """
        with self._lock:
            if not self._ingesting:
                return   # Drop packet — acquisition not started yet

            # ── Anchor t=0 to the first accepted sample ───────────────────
            
            if self._first_sample:
                self._start_time   = sample.timestamp
                self._first_sample = False

            # ── Append to circular buffers ────────────────────────────────
            
            self.timestamps.append(sample.timestamp)
            self.accel_x.append(sample.accel_x)
            self.accel_y.append(sample.accel_y)
            self.accel_z.append(sample.accel_z)
            self.magnitude.append(sample.magnitude)
            self.sample_count += 1

            # ── Update per-axis stats (O(1) incremental update) ───────────
            
            self.stats_x.update(sample.accel_x)
            self.stats_y.update(sample.accel_y)
            
            # Raw Z is stored as-is. The display negation (Z plots downward)
            # happens only in get_axis_stats() and in the plot tabs, never here.
            
            self.stats_z.update(sample.accel_z)

            # ── Update magnitude running summary ──────────────────────────
            
            m = sample.magnitude
            if m < self._mag_min: self._mag_min = m
            if m > self._mag_max: self._mag_max = m
            self._mag_n    += 1
            self._mag_mean += (m - self._mag_mean) / self._mag_n

            # ── Vibration threshold-crossing state machine ─────────────────
            #
            # LOGIC (rising-edge count with 7 ms debounce):
            #
            #  1. We only count on the RISING edge (False → True), never while
            #     the signal stays above the threshold. This prevents a sustained
            #     blast from being counted as thousands of separate "vibrations".
            #
            #  2. Even on a rising edge, we skip if we're still inside the 7 ms
            #     debounce window from the last counted event. This prevents a
            #     signal that bounces slightly below and back above the threshold
            #     within one vibration from being double-counted.
            #
            #  3. When the magnitude drops below the threshold we reset the
            #     _above_threshold flag so the next crossing can be counted.
            
            if m >= self.vibration_threshold:
                if not self._above_threshold:
                    
                    # Rising edge — check debounce window
                    
                    if (sample.timestamp - self._last_vib_time) >= VIB_DEBOUNCE_S:
                        self.vibration_count  += 1
                        self._last_vib_time    = sample.timestamp
                    self._above_threshold = True
            else:
                
                # Below threshold — arm the detector for the next rising edge
                
                self._above_threshold = False

            # ── CSV row write ─────────────────────────────────────────────
            
            if self.logging_active and self._csv_writer:
                try:
                    
                    # Time_s = elapsed seconds from START (t=0), NOT wall-clock.
                    # This matches what the CSV Viewer and plot tabs display.
                    
                    elapsed_s = sample.timestamp - self._start_time
                    self._csv_writer.writerow([
                        f"{elapsed_s:.4f}",
                        datetime.fromtimestamp(sample.timestamp)
                                .strftime("%Y-%m-%d %H:%M:%S.%f"),
                        f"{sample.accel_x:.4f}",
                        f"{sample.accel_y:.4f}",
                        f"{sample.accel_z:.4f}",    # raw, not negated
                        f"{sample.magnitude:.4f}",
                    ])
                    self._flush_ctr += 1
                    
                    # Flush to disk every CSV_FLUSH_N rows (50). Flushing on
                    # every row is safe but causes many tiny OS writes; never
                    # flushing risks losing data on crash/power loss.
                    
                    if self._flush_ctr >= CSV_FLUSH_N:
                        self._csv_file.flush()
                        self._flush_ctr = 0
                except OSError:
                    
                    # Disk full or file removed mid-session — silently skip this
                    # row. The operator can see logging status in the status bar.
                    
                    pass

    # ── Snapshots ─────────────────────────────────────────────────────────────

    def get_snapshot(self, n: int = MAX_BUFFER) -> dict:
        
        """
        Return a dict of numpy arrays containing the most recent *n* samples.

        WHY COPY TO NUMPY HERE:
        The deques are written by the background thread; the plot tabs read
        from the GUI thread. Converting to numpy inside the lock gives us an
        immutable snapshot that the plot code can work on without the lock
        being held for the (potentially slow) plot rendering.

        "start_time" is included so PlotManager can compute elapsed time
        correctly even after the deque has wrapped (snap["t"][0] would no
        longer be the session start after wrapping).

        "sample_count" is the absolute counter (never wraps) used by
        SeismicAnalyzer to detect truly new samples each refresh tick.
        """
        with self._lock:
            tail = lambda d: np.array(list(d)[-n:], dtype=np.float64)
            return {
                "t":            tail(self.timestamps),
                "ax":           tail(self.accel_x),
                "ay":           tail(self.accel_y),
                "az":           tail(self.accel_z),
                "mag":          tail(self.magnitude),
                "n":            min(n, len(self.timestamps)),
                "start_time":   self._start_time,
                "sample_count": self.sample_count,
            }

    def get_axis_stats(self) -> dict:
        
        """
        Return per-axis stats for the right sidebar display.

        Z SIGN CONVENTION:
        Raw Z is stored unsigned (positive = sensor face up, gravity reading
        ~+9.81 m/s²). For the sidebar display we negate current and mean so
        they match the inverted Z curve on the Acceleration plot tab.
        Z peak is NOT negated — it is an absolute maximum, always positive.
        The raw stored values and all magnitude/vibration calculations remain
        unaffected.
        """
        with self._lock:
            z = self.stats_z.as_dict()   # raw: signed current, abs peak, signed mean
            return {
                "x": self.stats_x.as_dict(),
                "y": self.stats_y.as_dict(),
                "z": {
                    "current": -z["current"],   # negated for display convention
                    "peak":     z["peak"],       # absolute max — always positive
                    "mean":    -z["mean"],       # negated for display convention
                },
            }

    def get_vibration_stats(self) -> dict:
        """Return vibration event statistics for the sidebar Vibration Monitor box."""
        with self._lock:
            ok = self._mag_n > 0
            return {
                "count": self.vibration_count,
                "min":   self._mag_min  if ok else 0.0,
                "max":   self._mag_max  if ok else 0.0,
                "mean":  self._mag_mean if ok else 0.0,
            }

    # ── CSV logging ───────────────────────────────────────────────────────────

    def start_logging(self, filepath: str) -> bool:
        
        """
        Open a new CSV file and start writing incoming samples.

        Returns True on success, False if the file could not be opened
        (e.g. permission error, disk full, invalid path).

        BUG-LOGFILE fix (v2.8.1):
        If start_logging() is somehow called while a file is already open
        (e.g. rapid toggle of the logging checkbox), we close the old handle
        first to prevent a file-handle leak. Under normal flow stop_logging()
        always precedes start_logging() so this guard is a no-op.
        """
        with self._lock:
            
            # Defensively close any already-open file before opening a new one.
            
            if self._csv_file is not None:
                try:
                    self._csv_file.flush()
                    self._csv_file.close()
                except OSError:
                    pass
                finally:
                    self._csv_file   = None
                    self._csv_writer = None
                    self.logging_active = False

            try:
                self._csv_path   = filepath
                
                # buffering=1 = line-buffered — every row ends with "\n" which
                # triggers an OS write. Combined with CSV_FLUSH_N-row flush()
                # calls, this ensures data is never silently stuck in a Python
                # buffer if the process crashes.
                
                self._csv_file   = open(filepath, "w", newline="", buffering=1)
                self._csv_writer = csv.writer(self._csv_file)
                
                # Write the header row that CSVViewerTab and external tools
                # like Excel will recognise.
                
                self._csv_writer.writerow([
                    "Time_s",          # elapsed seconds from session start (t=0)
                    "Timestamp",       # ISO-8601 wall-clock time for human reference
                    "Accel_X(m/s²)", "Accel_Y(m/s²)", "Accel_Z(m/s²)",
                    "Magnitude(m/s²)",
                ])
                self.logging_active = True
                self._flush_ctr     = 0
                return True
            except OSError:
                self.logging_active = False
                return False

    def stop_logging(self):
        
        """
        Flush and close the active CSV file.

        Safe to call even when logging is not active (the if-check guards it).
        Always called by DRDOController on STOP, CLEAR, or when the logging
        checkbox is unchecked mid-session.
        """
        with self._lock:
            self.logging_active = False
            if self._csv_file:
                try:
                    self._csv_file.flush()
                    self._csv_file.close()
                except OSError:
                    pass
                finally:
                    
                    # Always null out references so ingest() won't try to write
                    # to a closed file handle on the very next tick.
                    
                    self._csv_file   = None
                    self._csv_writer = None

    def save_snapshot_csv(self, filepath: str) -> bool:
        
        """
        Dump the entire current in-memory buffer to a CSV file in one shot.

        Unlike the streaming logger (start_logging / ingest), this writes
        everything we have at the moment the user clicks SAVE. Good for
        capturing a session retroactively even if logging was disabled.
        """
        with self._lock:
            n = len(self.timestamps)
            if n == 0:
                return False
            try:
                ts  = list(self.timestamps)
                axl = list(self.accel_x)
                ayl = list(self.accel_y)
                azl = list(self.accel_z)
                mgl = list(self.magnitude)
                
                # Use the stored session start time; fall back to the first
                # sample in the buffer if start_time was not set (should not
                # normally happen after v2.8 when set_ingesting was called).
                
                origin = self._start_time if self._start_time > 0.0 else ts[0]

                with open(filepath, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow([
                        "Time_s", "Timestamp",
                        "Accel_X(m/s²)", "Accel_Y(m/s²)",
                        "Accel_Z(m/s²)", "Magnitude(m/s²)",
                    ])
                    for i in range(n):
                        elapsed_s = ts[i] - origin
                        w.writerow([
                            f"{elapsed_s:.4f}",
                            datetime.fromtimestamp(ts[i])
                                    .strftime("%Y-%m-%d %H:%M:%S.%f"),
                            f"{axl[i]:.4f}", f"{ayl[i]:.4f}",
                            f"{azl[i]:.4f}", f"{mgl[i]:.4f}",
                        ])
                return True
            except OSError:
                return False

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def clear(self):
        
        """
        Full reset — wipes all buffers, stats, and counters.

        Called when the operator clicks CLEAR ALL. This also closes any open
        CSV file and resets the session time anchor so the next START produces
        a fresh t=0. DRDOController calls this followed by PlotManager.clear_plots().
        """
        with self._lock:
            self._ingesting = False
            self.stop_logging()   # close CSV if open (stop_logging re-acquires lock — RLock is re-entrant)

            for buf in (self.timestamps, self.accel_x, self.accel_y,
                        self.accel_z, self.magnitude):
                buf.clear()

            self.stats_x.reset()
            self.stats_y.reset()
            self.stats_z.reset()

            self.vibration_count  = 0
            self._above_threshold = False
            self._last_vib_time   = 0.0
            self.sample_count     = 0

            self._mag_min  = float("inf")
            self._mag_max  = float("-inf")
            self._mag_mean = 0.0
            self._mag_n    = 0

            # Reset time anchor so the next session starts its own fresh t=0
            
            self._start_time   = 0.0
            self._first_sample = True

    def set_vibration_threshold(self, value: float):
        
        """
        Update the vibration detection threshold in m/s².

        Clamps to 0.1 m/s² minimum to prevent the state machine from
        triggering on numerical noise. Resets the state machine immediately
        so the new threshold takes effect on the very next sample, without
        any stale debounce carry-over from the old threshold.
        """
        with self._lock:
            self.vibration_threshold = max(0.1, float(value))
            
            # Reset state machine so we start fresh with the new threshold.
            # Without this, a debounce window from the old threshold could
            # silently suppress the first crossing under the new threshold.
            
            self._above_threshold = False
            self.vibration_count  = 0
            self._last_vib_time   = 0.0
