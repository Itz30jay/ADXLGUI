"""
DRDO PXE — ADXL345 Data Acquisition System
connection_handler.py  |  v2.0.2

Author  : Jaykishan
Version : 2.0.2
Org     : Defence Research & Development Organisation — PXE

WHAT THIS FILE DOES
-------------------
Acts as a clean, stable interface between the GUI controller (main.py) and
the low-level UDP networking code (udp_worker.py).

WHY A FACADE?
-------------
Without this class, main.py would have to talk to UdpWorker directly —
meaning every other part of the GUI would need to know about threads,
socket lifecycles, and reconnect logic. ConnectionHandler hides all of that.
The rest of the GUI only calls connect_udp(), disconnect(), and is_connected(),
and listens to four simple signals. UdpWorker can be replaced (e.g. with a
serial port worker) without touching any other file.

SIGNAL FORWARDING (direct, not bounce methods)
----------------------------------------------
Older versions had _on_data() and _on_status() "bounce" methods that received
a signal from UdpWorker and immediately re-emitted it as another signal.
Those middle-man methods added two extra function calls per incoming packet
(~133 times per second) with zero benefit.
v2.0.2 connects worker signals directly to our own signals — PyQt6 handles the
forwarding in C++, not Python, so it is essentially free.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from data_handler import DataHandler
from udp_worker   import UdpWorker


class ConnectionHandler(QObject):
    """
    Thin facade that owns exactly one UdpWorker thread at a time.

    The GUI controller (DRDOController in main.py) only talks to this class.
    All UDP socket details, threading, and reconnect logic are hidden here.

    Signals (re-emitted directly from UdpWorker — see _wire_worker)
    ----------------------------------------------------------------
    data_received(SensorSample)
        Fired every time a valid 6-byte ADXL345 packet arrives.
        Even when the DataHandler ingestion gate is closed (i.e. before
        START is clicked), this signal fires so main.py can track the raw
        byte rate and confirm the sensor is streaming.

    connection_status(bool, str)
        (is_connected, human-readable message) — used to drive the LED
        indicator, status bar text, and button enable/disable states.

    error_occurred(str)
        Non-fatal status-bar messages (e.g. "too many parse errors").

    reconnecting(int)
        Emitted with the attempt number during auto-reconnect so the UI
        can show "Reconnecting (3/10)…".
    """

    # ── Signals ───────────────────────────────────────────────────────────────
    
    # These match UdpWorker's signals exactly, allowing direct forwarding.
    
    data_received     = pyqtSignal(object)      # SensorSample namedtuple
    connection_status = pyqtSignal(bool, str)   # (connected, message)
    error_occurred    = pyqtSignal(str)
    reconnecting      = pyqtSignal(int)         # retry attempt number

    def __init__(self, data_handler: DataHandler, parent=None):
        super().__init__(parent)
        
        # Keep a reference to the shared DataHandler so we can pass it to
        # whichever UdpWorker we spawn. DataHandler is thread-safe (RLock),
        # so the worker thread can call ingest() safely.
        
        self._data_handler = data_handler
        
        # Start with no worker. _worker is set only after connect_udp().
        
        self._worker: UdpWorker | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    def connect_udp(self, port: int = 8888, auto_reconnect: bool = True):
        
        """
        Bind a UDP socket on *port* and start listening for ADXL345 packets.

        If a worker is already running (e.g. reconnecting with a new port),
        _stop_worker() tears it down cleanly before starting a new one.
        The DataHandler is NOT cleared here — that is the controller's job
        so the user can reconnect without losing buffered data.
        """
        # Always tear down any existing worker first to avoid two sockets
        # competing for the same port, which would cause an OS bind error.
        
        self._stop_worker()

        worker = UdpWorker(
            port=port,
            data_handler=self._data_handler,
            auto_reconnect=auto_reconnect,
        )
        
        # Wire signals before starting the thread so we never miss the very
        # first connection_status emission that UdpWorker fires when it binds.
        
        self._wire_worker(worker)
        self._worker = worker
        self._worker.start()   # QThread.start() → calls run() in the new thread

    def disconnect(self):
        
        """
        Stop the UDP worker and announce disconnection.

        The controller calls _on_stop() before calling this when acquisition
        is active, so we don't need to worry about stopping logging here.
        After _stop_worker() the connection_status(False, …) emission lets
        the UI re-enable the CONNECT button automatically.
        """
        self._stop_worker()
        self.connection_status.emit(False, "Disconnected by operator.")

    def is_connected(self) -> bool:
        
        """
        Returns True if the UDP worker thread is alive.

        Used by DRDOController._on_start() to guard against clicking START
        without a live connection. QThread.isRunning() is thread-safe.
        """
        return self._worker is not None and self._worker.isRunning()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _wire_worker(self, worker: UdpWorker):
        
        """
        Connect UdpWorker's signals directly to our own signals.

        WHY DIRECT (not bounce methods):
        Previously there were _on_data() and _on_status() methods here that
        just did `self.data_received.emit(sample)`. This created unnecessary
        Python function call overhead at 133 Hz. Direct signal-to-signal
        connections are handled by Qt's C++ internals and cost almost nothing.
        """
        worker.data_received.connect(self.data_received)
        worker.connection_status.connect(self.connection_status)
        worker.error_occurred.connect(self.error_occurred)
        worker.reconnecting.connect(self.reconnecting)

    def _stop_worker(self):
        
        """
        Gracefully stop the running worker thread.

        UdpWorker.stop() sets _running=False and closes the socket, which
        unblocks any pending recvfrom() call so the thread exits quickly.
        We then null out _worker so is_connected() returns False immediately.
        """
        
        if self._worker is not None:
            self._worker.stop()   # signals thread to exit, waits up to 3 s
            self._worker = None
