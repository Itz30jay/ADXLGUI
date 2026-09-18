"""
DRDO PXE — ADXL345 Data Acquisition System
main.py  |  v2.2.0

WHAT THIS FILE DOES
-------------------
Application entry point and MVC controller.

DRDOController wires three independent subsystems together:
    DataHandler       — owns all buffered sensor data (the Model)
    ConnectionHandler — owns the UDP socket thread (networking)
    MainWindow        — the entire Qt GUI layout (the View)
    PlotManager       — live pyqtgraph plots (injected into the View)

ACQUISITION STATE MACHINE
--------------------------
CONNECT  → UDP socket binds; LED goes green; ingestion gate stays closed
START    → ingestion gate opens; CSV file opens; plots start refreshing
STOP     → ingestion gate closes; CSV file flushed and closed; plots freeze
SAVE     → dump current in-memory buffer to a new CSV (any time)
CLEAR    → wipe all buffers, plots, and stats; close CSV if open

WHY TWO TIMERS?
---------------
_plot_timer  (80 ms, 12.5 Hz) — refreshes pyqtgraph curves.
    We cap at 12.5 Hz even though data arrives at ~133 Hz because:
      a) pyqtgraph re-renders the entire curve on every call, which takes
         ~3-5 ms. Calling it at 133 Hz would consume ~40 % of a single CPU core.
      b) Human eyes cannot perceive updates faster than ~24 Hz anyway.
    At 12.5 Hz each frame gets ~10-11 new data points to add.

_status_timer (500 ms, 2 Hz) — refreshes text: axis stats, byte rate, Hz rate.
    Stats and rates are sampled quantities (we compare "now" vs "500 ms ago").
    Running them at 80 ms would give noisy one-frame-window samples.
    500 ms gives stable, readable numbers.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

from PyQt6.QtCore    import QTimer
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

import pyqtgraph as pg

from data_handler       import DataHandler
from connection_handler import ConnectionHandler
from plot_manager       import PlotManager
from ui_mainwindow      import MainWindow

# R must be imported before any widget is constructed.
# R.init() is called in main() immediately after QApplication so screen
# metrics are available for every subsequent widget dimension calculation.

from responsive         import R

# Antialiasing gives smoother curves. OpenGL is disabled because it can cause
# driver-specific rendering artefacts on some DRDO lab PCs. crashWarning
# suppresses a non-fatal PyQtGraph internals warning on Python 3.12.

pg.setConfigOptions(antialias=True, useOpenGL=False, crashWarning=False)

# Plot tab refresh: 80 ms = 12.5 Hz. Fast enough to feel real-time; slow
# enough that pyqtgraph render cost doesn't monopolise the main thread.

PLOT_REFRESH_MS  = 80

# Status bar refresh: 500 ms = 2 Hz. Gives stable, averaged byte-rate and
# sample-rate numbers that are easy to read without flickering.

STATUS_UPDATE_MS = 500


class DRDOController:
    
    """
    Application controller — the "C" in MVC.

    Owns one of each subsystem:
        DataHandler       → shared with ConnectionHandler (UdpWorker) and PlotManager
        ConnectionHandler → owns the UdpWorker background thread
        MainWindow        → the Qt widget tree; receives display commands from us
        PlotManager       → pyqtgraph tab widget injected into MainWindow

    The controller's job is signal wiring, state transitions, and coordinating
    the above components. It contains NO data processing and NO widget building.
    """

    def __init__(self, app: QApplication):
        
        # ── Subsystem construction ──────────────────────────────────────────
        
        # DataHandler is created first because both ConnectionHandler and
        # PlotManager need a reference to the same shared instance.
        
        self._data = DataHandler()

        # ConnectionHandler wraps UdpWorker; pass data_handler so ingest()
        # is called directly from the background thread with zero overhead.
        
        self._conn = ConnectionHandler(data_handler=self._data)

        # Build and show the Qt main window.
        
        self._win  = MainWindow()

        # PlotManager is a QTabWidget injected into the blank centre area of
        # MainWindow. Injecting (vs building inside MainWindow) keeps the
        # plotting code fully decoupled from the window layout code.
        
        self._plots = PlotManager(data_handler=self._data)
        self._win.inject_plot_widget(self._plots)

        # ── Controller state ────────────────────────────────────────────────
        
        # _acquiring tracks whether data is actually flowing into DataHandler.
        # It is set True by START, False by STOP/CLEAR/disconnect.
        
        self._acquiring       = False

        # Output folder for CSV files (updated by the browse button).
        
        self._output_folder   = os.path.expanduser("~")
        
        # Full path of the currently open (or most recently closed) CSV.
        
        self._csv_path        = ""

        # Byte and sample counters for the status bar rate calculations.
        # Both are compared against their previous-tick values (delta / dt).
        
        self._bytes_recv      = 0        # total UDP bytes seen (all packets, all time)
        self._last_bytes      = 0        # value at the last status-timer tick
        self._last_rate_t     = time.time()
        self._last_sample_cnt = 0        # DataHandler.sample_count at last tick

        # ── Wire everything together ────────────────────────────────────────
        
        self._wire_ui()      # button clicks → controller methods
        self._wire_conn()    # ConnectionHandler signals → controller methods

        # ── Start timers ───────────────────────────────────────────────────
        
        self._plot_timer = QTimer()
        self._plot_timer.timeout.connect(self._on_plot_tick)
        self._plot_timer.start(PLOT_REFRESH_MS)

        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._on_status_tick)
        self._status_timer.start(STATUS_UPDATE_MS)

        # Populate the filename preview label with today's date immediately
        # so the operator can see where their data will be saved.
        
        self._update_filename_preview()

    # ── Signal wiring ──────────────────────────────────────────────────────────

    def _wire_ui(self):
        
        """
        Connect every button and checkbox in MainWindow to a controller slot.

        WHY here (not in MainWindow)?
        MainWindow is a "dumb" view — it builds widgets but does not know
        what they do. The controller decides what each control means and wires
        it here. This separation means the view can be restyled or rebuilt
        without touching any business logic.
        """
        w = self._win
        w.btn_connect_udp.clicked.connect(self._on_connect_udp)
        w.btn_disconnect.clicked.connect(self._on_disconnect)
        w.btn_start.clicked.connect(self._on_start)
        w.btn_stop.clicked.connect(self._on_stop)
        w.btn_save.clicked.connect(self._on_save)
        w.btn_clear.clicked.connect(self._on_clear)
        w.btn_browse.clicked.connect(self._on_browse)
        # Filename preview updates whenever the operator name changes.
        w.edit_username.textChanged.connect(lambda _: self._update_filename_preview())
        w.edit_folder.textChanged.connect(self._on_folder_changed)
        w.chk_logging.toggled.connect(self._on_logging_toggled)
        
        # Pause checkbox feeds directly into PlotManager.set_paused(bool).
        # All four plot tabs respect this flag.
        
        w.chk_pause_plot.toggled.connect(self._plots.set_paused)

    def _wire_conn(self):
        
        """
        Connect ConnectionHandler signals to controller handlers.

        These four signals cover the entire connection lifecycle and are the
        only channel through which networking events reach the GUI.
        """
        self._conn.data_received.connect(self._on_data_received)
        self._conn.connection_status.connect(self._on_connection_status)
        self._conn.error_occurred.connect(self._on_conn_error)
        self._conn.reconnecting.connect(self._on_reconnecting)

    # ── Connection handlers ────────────────────────────────────────────────────

    def _on_connect_udp(self):
        
        """
        CONNECT UDP button → bind UDP socket and start listening.

        DataHandler.clear() is called BEFORE connecting so that whatever stale
        data was in the buffer from a previous session does not confuse the
        plots or statistics in the new session.

        The ingestion gate (_data.set_ingesting) stays False until START —
        packets can flow through the socket but are counted (byte rate display)
        without being stored in the ring buffer.
        """
        port = self._win.spin_udp_port.value()
        auto = self._win.chk_auto_reconnect_udp.isChecked()
        self._data.clear()
        
        # Disable the CONNECT button immediately to prevent double-clicks
        # that would try to bind the same port twice.
        
        self._win.btn_connect_udp.setEnabled(False)
        self._conn.connect_udp(port=port, auto_reconnect=auto)

    def _on_disconnect(self):
        
        """
        DISCONNECT button → stop acquisition if running, then close socket.

        Calling _on_stop() first ensures the CSV is flushed and the ingestion
        gate is closed before the socket thread exits. If we closed the socket
        first, a race condition could leave the last few packets' data in an
        inconsistent state.
        """
        if self._acquiring:
            self._on_stop()
            
        # ConnectionHandler.disconnect() emits connection_status(False, …)
        # which triggers _on_connection_status. That handler re-enables the
        # CONNECT button and resets the LED — no explicit calls needed here.
        
        self._conn.disconnect()

    def _on_connection_status(self, connected: bool, msg: str):
        
        """
        Called whenever the UDP socket connects, disconnects, or errors.

        WHY update the UI from here (not from ConnectionHandler)?
        ConnectionHandler should not know about UI widgets. The controller
        translates the (bool, str) signal into specific UI state changes.
        """
        self._win.update_connection_ui(connected, msg)
        if connected:
            port = self._win.spin_udp_port.value()
            self._win.sb_conn_type.setText(f"UDP:{port}")
            self._win.sb_msg.setText(
                f"UDP listening on port {port} — press START to acquire data")
        else:
            
            # Re-enable CONNECT so the operator can try again.
            
            self._win.btn_connect_udp.setEnabled(True)
            
            # If the connection dropped while acquiring (network cable pulled),
            # stop cleanly rather than leaving the GUI in a half-started state.
            
            if self._acquiring:
                self._on_stop()

    def _on_conn_error(self, msg: str):
        
        """Non-fatal error (e.g. one bad packet) → show in status bar only."""
        
        self._win.sb_msg.setText(f"⚠ {msg}")

    def _on_reconnecting(self, attempt: int):
        
        """Update the status indicator during auto-reconnect back-off."""
        
        self._win.update_connection_ui(False, f"Reconnecting ({attempt}/10)…")

    # ── Acquisition handlers ───────────────────────────────────────────────────

    def _on_start(self):
        
        """
        START ACQUISITION button.

        Guards:
          1. Must have a live UDP connection first.
          2. If CSV logging is requested, opens the file (reports error but
             continues acquisition — data is still buffered in RAM).

        After this call DataHandler.ingest() stores every arriving packet.
        """
        if not self._conn.is_connected():
            self._win.show_message(
                "Not Connected",
                "No active UDP connection.\n"
                "Click  CONNECT UDP  in the sidebar first.",
                "warning")
            return

        self._acquiring = True
        self._win.set_acquisition_active(True)

        # Open the ingestion gate — packets now flow into the ring buffers.
        
        self._data.set_ingesting(True)

        # Build the CSV filename once at START rather than at CONNECT, so
        # the timestamp in the filename matches the actual data start time.
        
        self._update_filename_preview()
        user      = self._win.edit_username.text().strip() or "User"
        ts        = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        fname     = f"ADXL345_{user}_{ts}.csv"
        self._csv_path = os.path.join(self._output_folder, fname)

        if self._win.chk_logging.isChecked():
            ok = self._data.start_logging(self._csv_path)
            if not ok:
                self._win.show_message(
                    "Logging Error",
                    f"Cannot write:\n{self._csv_path}\n\n"
                    "Acquisition continues without logging.",
                    "warning")
                self._csv_path = ""   # clear so status bar shows "—"

        self._win.sb_msg.setStyleSheet(
            "color:#00FF88;background:transparent;padding:0 8px;")
        self._win.sb_msg.setText("● ACQUIRING — data flowing")

    def _on_stop(self):
        
        """
        STOP ACQUISITION button (also called internally by disconnect/clear).

        Order matters:
          1. Close ingestion gate first (stops new samples from entering).
          2. Flush and close CSV (so the file is complete before the OS can
             cache or buffer any trailing data).
          3. Update UI state.
        """
        self._acquiring = False
        self._data.set_ingesting(False)    # close gate — no more samples stored
        self._data.stop_logging()          # flush + close CSV
        self._win.set_acquisition_active(False)
        self._win.sb_msg.setStyleSheet(
            "color:#FF9900;background:transparent;padding:0 8px;")
        self._win.sb_msg.setText("■ STOPPED — data frozen")

    def _on_save(self):
        
        """
        SAVE DATA button — dump the entire in-memory buffer to a new CSV.

        This is different from the real-time streaming logger. save_snapshot_csv()
        writes all data currently in the ring buffer in a single pass. Useful
        when the operator forgot to enable logging before clicking START.
        """
        if self._data.sample_count == 0:
            self._win.show_message("No Data", "No samples in buffer.", "info")
            return
        user    = self._win.edit_username.text().strip() or "User"
        ts      = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        default = os.path.join(
            self._output_folder, f"ADXL345_{user}_snapshot_{ts}.csv")
        path, _ = QFileDialog.getSaveFileName(
            self._win, "Save CSV", default, "CSV Files (*.csv)")
        if not path:
            return    # operator cancelled the dialog
        ok = self._data.save_snapshot_csv(path)
        if ok:
            self._win.show_message("Saved", f"Snapshot saved:\n{path}", "info")
        else:
            self._win.show_message("Error", f"Failed to write:\n{path}", "error")

    def _on_clear(self):
        
        """
        CLEAR ALL button — wipe all data, plots, and statistics.

        Confirmation dialog prevents accidental data loss.
        After clearing, the zero-stats panels are pushed explicitly so the
        sidebar does not show stale values from the previous session.
        """
        if QMessageBox.question(
                self._win, "Confirm Clear",
                "Clear ALL buffered data, stats, and graphs?\nCannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return

        if self._acquiring:
            self._on_stop()

        self._data.clear()          # resets buffers, stats, CSV, ingestion gate
        self._plots.clear_plots()   # wipes all four plot tabs
        self._csv_path        = ""
        self._last_sample_cnt = 0
        self._bytes_recv      = 0
        self._last_bytes      = 0

        # Push zeroed stat panels immediately. Without this, the status timer
        # fires 500 ms later and the sidebar briefly shows the previous session's
        # values — confusing if the operator starts a new session quickly.
        
        zero = {ax: {"current": 0., "peak": 0., "mean": 0.}
                for ax in ("x", "y", "z")}
        self._win.update_stats_panel(zero)
        self._win.update_vibration_box({"count": 0, "min": 0., "max": 0., "mean": 0.})
        self._win.update_status_bar(0, False, "", 0, "0 B/s", "— Hz")
        self._win.sb_msg.setStyleSheet(
            "color:#6A8AAA;background:transparent;padding:0 8px;")
        self._win.sb_msg.setText("Buffer cleared — press START to acquire")

    def _on_logging_toggled(self, enabled: bool):
        
        """
        Called when the operator toggles the "Enable Real-time CSV Logging"
        checkbox WHILE an acquisition session is already running.

        BUG-D fix: always generate a NEW filename when re-enabling logging.
        DataHandler.start_logging() opens with mode="w" (write-from-scratch),
        so reusing the old self._csv_path would silently truncate and overwrite
        whatever was already logged in this session. A fresh timestamp ensures
        every logging segment is a distinct, complete file.
        """
        if not self._acquiring:
            return    # checkbox change before START → no-op
        if enabled:
            user           = self._win.edit_username.text().strip() or "User"
            ts             = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self._csv_path = os.path.join(
                self._output_folder, f"ADXL345_{user}_{ts}.csv")
            ok = self._data.start_logging(self._csv_path)
            if not ok:
                self._win.show_message(
                    "Logging Error",
                    f"Cannot write:\n{self._csv_path}\n\nLogging not started.",
                    "warning")
                self._csv_path = ""
        else:
            self._data.stop_logging()

    # ── File helpers ───────────────────────────────────────────────────────────

    def _on_browse(self):
        
        """Browse button → let the operator pick an output folder for CSV files."""
        
        folder = QFileDialog.getExistingDirectory(
            self._win, "Select Output Folder", self._output_folder)
        if folder:
            self._output_folder = folder
            self._win.edit_folder.setText(folder)

    def _on_folder_changed(self, path: str):
        
        """
        Called when the operator types a path directly into the folder text box.
        Only update _output_folder if the path is a real, accessible directory.
        """
        if os.path.isdir(path):
            self._output_folder = path
        self._update_filename_preview()

    def _update_filename_preview(self):
        
        """
        Show the operator what the next CSV will be called.

        Re-computed on every username or folder change so the preview is
        always accurate. The actual file is only created when START is clicked.
        """
        user = self._win.edit_username.text().strip() or "User"
        ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._win.update_filename_preview(f"ADXL345_{user}_{ts}.csv")
        self._win.lbl_user.setText(user)

    # ── Data received callback ─────────────────────────────────────────────────

    def _on_data_received(self, _sample):
        
        """
        Fired by ConnectionHandler for every UDP packet (before ingestion gate check).

        WHY track bytes here even when not ingesting?
        The byte-rate counter (_bytes_recv) feeds the "Data: 0.8 KB/s" status
        bar field. This field should show the live stream rate even when the
        operator has not clicked START yet — so they can confirm the sensor is
        streaming before beginning a logged session.

        We add PACKET_SIZE (6 bytes) per packet regardless of whether
        DataHandler actually stored the sample.
        """
        self._bytes_recv += 6    # each ADXL345 UDP packet is exactly 6 bytes

    # ── Timers ─────────────────────────────────────────────────────────────────

    def _on_plot_tick(self):
        
        """
        12.5 Hz plot refresh. Calls PlotManager.refresh() which reads a
        snapshot from DataHandler and redraws all three live plot tabs.

        Guard: only refresh when actively acquiring AND there is data.
        The CSV Viewer tab is not included here — it has no live feed.
        """
        if self._acquiring and self._data.sample_count > 0:
            self._plots.refresh()

    def _on_status_tick(self):
        
        """
        2 Hz status update — computes derived rates and pushes to the UI.

        RATES (Hz and KB/s) are computed as delta/dt rather than snapshots
        of instantaneous values, giving stable, averaged numbers even when
        the arrival rate varies slightly packet-to-packet.
        """
        # Push fresh axis stats and vibration event count to the right sidebar.
        
        self._win.update_stats_panel(self._data.get_axis_stats())
        self._win.update_vibration_box(self._data.get_vibration_stats())

        # Compute rates over the interval since the last tick.
        
        now = time.time()
        dt  = now - self._last_rate_t
        cnt = self._data.sample_count
        if dt > 0:
            hz_s   = f"{(cnt - self._last_sample_cnt) / dt:.1f} Hz"
            br     = (self._bytes_recv - self._last_bytes) / dt
            rate_s = f"{br / 1024:.1f} KB/s" if br >= 1024 else f"{br:.0f} B/s"
        else:
            hz_s   = "— Hz"
            rate_s = "0 B/s"

        # Store current values so the NEXT tick can compute the delta.
        
        self._last_sample_cnt = cnt
        self._last_bytes      = self._bytes_recv
        self._last_rate_t     = now

        self._win.update_status_bar(
            samples   = cnt,
            logging   = self._data.logging_active,
            file      = self._csv_path,
            vib_count = self._data.vibration_count,
            data_rate = rate_s,
            sample_hz = hz_s,
        )
        self._update_filename_preview()

    def show(self):
        
        """Show the main window. Called by main() after the controller is built."""
        
        self._win.show()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    
    # Enable high-DPI scaling before creating the QApplication — this must be
    # set as an environment variable on Qt 6.2 and earlier to take effect.
    
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv)
    app.setApplicationName("DRDO PXE DAQ — ADXL345 v3.0")

    # Initialise the responsive scaling engine BEFORE any widget is built.
    # R.init() reads the primary screen's available geometry and logical DPI,
    # computes the unified scale factor, and caches it for the session lifetime.
    
    R.init()

    # Global stylesheet overrides for elements that appear in multiple widgets:
    # tooltips (dark background with gold text matches the military-blue theme)
    # and the vertical scrollbar in the left sidebar.
    
    app.setStyleSheet("""
        QToolTip {
            background: #0D1F35; color: #FFD700;
            border: 1px solid #1E4A7A; font: 8pt Consolas; padding: 3px;
        }
        QScrollBar:vertical {
            background: #091828; width: 7px; border-radius: 3px;
        }
        QScrollBar::handle:vertical {
            background: #1E4A7A; border-radius: 3px; min-height: 20px;
        }
        QScrollBar::handle:vertical:hover { background: #2E6AAA; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar:horizontal { height: 0; }
    """)

    ctrl = DRDOController(app)
    ctrl.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
