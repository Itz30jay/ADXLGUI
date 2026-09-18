"""
DRDO PXE — ADXL345 Data Acquisition System
udp_worker.py  |  v2.0.0

WHAT THIS FILE DOES
-------------------
Runs a dedicated background thread (QThread) that:
  1. Binds a UDP socket on a configurable port
  2. Listens for 6-byte ADXL345 datagrams from the microcontroller
  3. Unpacks the binary struct, converts raw integers to m/s², creates a
     SensorSample, and hands it to DataHandler.ingest()
  4. Re-emits the same sample as a Qt signal so ConnectionHandler can
     forward it to the GUI (byte-rate counter, etc.)
  5. Optionally auto-reconnects if the socket dies (OS error, network glitch)

WHY A QTHREAD (not asyncio / threading.Thread)?
------------------------------------------------
We are inside a PyQt6 application. QThread integrates with Qt's event loop
and signal/slot mechanism so we can safely emit signals from the background
thread and have them delivered to GUI objects in the main thread without any
manual queue or mutex.  asyncio would require an event loop bridge; a plain
threading.Thread cannot safely emit Qt signals.

UDP PACKET FORMAT (Arduino / ESP32 firmware sends this):
    struct Packet {
        int16_t x;   // accel_x × 100  →  e.g. int 981 = 9.81 m/s²
        int16_t y;   // accel_y × 100
        int16_t z;   // accel_z × 100
    };
Total: 6 bytes, little-endian (default for Arduino / AVR / ESP32).

WHY MULTIPLY BY 100 (not the raw 0.0039 g/LSB scale)?
------------------------------------------------------
The firmware converts the raw ADXL345 readings to m/s² first (in floating
point on the MCU), then scales the result to an integer by multiplying by
100 before packing into int16.  This avoids sending floats over UDP (which
would need 12 bytes) while preserving 2 decimal places of precision
(e.g. 9.81 m/s² → 981 → 9.81 m/s² after dividing by 100).
"""

from __future__ import annotations

import struct
import socket
import time

from PyQt6.QtCore import QThread, pyqtSignal

from data_handler import DataHandler, SensorSample

# ── Configuration constants ────────────────────────────────────────────────────

# "0.0.0.0" means "bind on every available network interface" so the GUI works
# regardless of whether the PC is connected via Ethernet, Wi-Fi, or both.

UDP_LISTEN_IP     = "0.0.0.0"

DEFAULT_UDP_PORT  = 8888       # Must match the port in the Arduino firmware

# settimeout(1.0) means recvfrom() blocks for at most 1 second before raising
# socket.timeout. This lets the while-loop check self._running once per second
# even when no packets arrive — so stop() can exit the thread cleanly.

SOCKET_TIMEOUT_S  = 1.0

# "<hhh" = little-endian, three signed 16-bit integers.
# "<" = little-endian (Arduino/ESP32 native byte order)
# "h" = signed short (int16_t, 2 bytes each)

PACKET_FORMAT     = "<hhh"
PACKET_SIZE       = struct.calcsize(PACKET_FORMAT)    # 6 bytes

# The firmware multiplies m/s² × 100 before packing. We divide by 100 to
# recover the original floating-point acceleration value.

SCALE_FACTOR      = 100.0

# After MAX_PARSE_ERR consecutive bad packets we stop and report the error.
# This prevents a firmware bug from silently flooding the log with garbage
# while the GUI appears to work. 50 = ~0.4 seconds of bad data at 133 Hz.

MAX_PARSE_ERR     = 50

# Auto-reconnect limits. 10 retries × 2 seconds each = up to 20 s before
# giving up. These are reset to 0 on every successful bind.

MAX_RECONNECT     = 10
RECONNECT_DELAY_S = 2.0


class UdpWorker(QThread):
    
    """
    Background thread that drives the entire UDP receive pipeline.

    Lifecycle:
        UdpWorker(port, data_handler) → .start() → runs run() in new thread
        stop() → sets _running=False, closes socket → thread exits → wait()

    Signals (emitted from the background thread, delivered to GUI thread by Qt):
        data_received(SensorSample)   — one valid decoded accelerometer reading
        connection_status(bool, str)  — (is_live, human message)
        error_occurred(str)           — non-fatal warning for status bar
        reconnecting(int)             — attempt number during auto-reconnect
    """

    # Qt signals — emitting these from the background thread is safe because
    # Qt's signal/slot mechanism automatically posts them to the correct thread.
    
    data_received     = pyqtSignal(object)       # SensorSample namedtuple
    connection_status = pyqtSignal(bool, str)    # (connected_bool, message_str)
    error_occurred    = pyqtSignal(str)
    reconnecting      = pyqtSignal(int)          # retry attempt number 1..10

    def __init__(
        self,
        port:           int         = DEFAULT_UDP_PORT,
        data_handler:   DataHandler = None,
        auto_reconnect: bool        = True,
        parent=None,
    ):
        
        # Raise immediately if data_handler is missing, rather than crashing
        # silently on the first packet inside the background thread where the
        # exception would be harder to diagnose.
        
        if data_handler is None:
            raise ValueError(
                "UdpWorker requires a DataHandler instance. "
                "Pass data_handler=<DataHandler> when constructing.")

        super().__init__(parent)
        self.port           = port
        self.data_handler   = data_handler
        self.auto_reconnect = auto_reconnect
        self._running       = False
        
        # Socket is created fresh on each bind attempt and torn down by _close_socket().
        
        self._sock: socket.socket | None = None

    # ── Public control ─────────────────────────────────────────────────────────

    def stop(self):
        
        """
        Signal the thread to exit and unblock any blocking recvfrom() call.

        WHY close the socket HERE (from the main thread)?
        recvfrom() blocks until a packet arrives or the 1-second timeout fires.
        Closing the socket from outside the thread raises an OSError inside
        recvfrom() immediately, which is caught and triggers a clean exit.
        Without this, stop() would need to wait up to 1 second for the next
        timeout before the thread checks _running. Socket close = instant exit.
        """
        self._running = False
        self._close_socket()   # interrupts any blocking recvfrom() immediately
        self.wait(3000)        # give the thread up to 3 seconds to fully exit

    # ── Thread entry point ─────────────────────────────────────────────────────

    def run(self):
        
        """
        Main loop of the background thread. Qt calls this after start().

        STRUCTURE:
            outer while → tries to bind → if bind fails, retry or give up
                         → if bind succeeds, enter _recv_loop (inner tight loop)
                         → if _recv_loop exits due to socket error, retry bind
        """
        
        self._running = True
        attempt       = 0

        while self._running:
            
            # Try to bind the socket. _bind() returns False on failure.
            
            if not self._bind():
                attempt += 1
                if not self.auto_reconnect or attempt > MAX_RECONNECT:
                    self.connection_status.emit(
                        False, "UDP bind failed — max retries reached.")
                    break
                self.reconnecting.emit(attempt)   # tells GUI "Reconnecting (3/10)…"
                time.sleep(RECONNECT_DELAY_S)
                continue

            # Bind succeeded — reset the counter so retries after a future
            # disconnect start fresh from 1, not from wherever we left off.
            
            attempt = 0
            self._recv_loop()    # blocks here until a socket error or stop()

            if self._running and self.auto_reconnect:
                
                # The socket died (network cable pulled, etc.) but the user
                # has not clicked DISCONNECT. Try to re-bind automatically.
                
                self.connection_status.emit(False, "UDP socket error — retrying bind…")
                time.sleep(RECONNECT_DELAY_S)
            else:
                break    # either stop() was called or auto_reconnect is off

        self._close_socket()
        self.connection_status.emit(False, "UDP listener stopped.")

    # ── Socket lifecycle ───────────────────────────────────────────────────────

    def _bind(self) -> bool:
        
        """
        Create a fresh UDP socket and bind it to the configured port.

        WHY SO_REUSEADDR?
        Without SO_REUSEADDR, if the GUI crashes (or the user kills it) and
        reopens within ~60 seconds, the OS still has the port in TIME_WAIT
        state and the new bind() raises "Address already in use". REUSEADDR
        lets us rebind immediately.

        Returns True on success, False on failure (caller handles retry logic).
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(SOCKET_TIMEOUT_S)    # so _recv_loop can check _running
            sock.bind((UDP_LISTEN_IP, self.port))
            self._sock = sock
            self.connection_status.emit(
                True, f"UDP listening on {UDP_LISTEN_IP}:{self.port}")
            return True
        except OSError as exc:
            self.connection_status.emit(False, f"UDP bind error: {exc}")
            self._close_socket()
            return False

    def _close_socket(self):
        
        """
        Close the socket and null the reference. Safe to call even if already closed.

        WHY the try/except?
        If stop() closes the socket while _recv_loop is inside recvfrom(), the
        OS raises OSError("Bad file descriptor") on the close() call itself on
        some platforms. We catch and discard it — we only care that the socket
        ends up closed.
        """
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ── Inner receive loop ─────────────────────────────────────────────────────

    def _recv_loop(self):
        
        """
        The tight hot path: receive datagrams, parse them, feed the pipeline.

        PERFORMANCE NOTE:
        This runs at ~133 iterations per second (the Arduino sends ~133 packets/s).
        Every line of code here executes 133 × per second. We keep it minimal:
          • No logging
          • No string formatting except on error paths
          • parse_errors counter lives in a local variable (faster than self.x)

        EXIT CONDITIONS:
          1. self._running becomes False (stop() was called)
          2. socket.timeout exception (normal — loop re-checks _running)
          3. OSError exception (unexpected — exits, outer loop may retry)
          4. parse_errors exceeds MAX_PARSE_ERR (firmware bug protection)
        """
        parse_errors = 0

        while self._running:

            # ── Step 1: Receive raw bytes ──────────────────────────────────
            
            try:
                data, _addr = self._sock.recvfrom(64)
                
                # 64 bytes is intentionally larger than PACKET_SIZE (6) so
                # the OS won't truncate packets if the firmware ever adds a
                # header or checksum in the future.
                
            except socket.timeout:
                
                # Perfectly normal — 1-second tick lets us check _running.
                
                continue
            except OSError as exc:
                if self._running:
                    
                    # Only report if we weren't deliberately stopped by stop().
                    
                    self.error_occurred.emit(f"UDP recv error: {exc}")
                break    # Let the outer loop decide whether to retry

            # ── Step 2: Size validation ────────────────────────────────────
            
            if len(data) < PACKET_SIZE:
                
                # Undersized packet — firmware bug, wrong IP/port, or noise.
                # We tolerate a burst (MAX_PARSE_ERR) before aborting.
                
                parse_errors += 1
                if parse_errors > MAX_PARSE_ERR:
                    self.error_occurred.emit(
                        "Too many undersized UDP packets — check firmware.")
                    break
                continue

            # ── Step 3: Parse binary struct → SensorSample ────────────────
            
            sample = self._parse_packet(data)
            if sample is None:
                parse_errors += 1
                if parse_errors > MAX_PARSE_ERR:
                    self.error_occurred.emit("Too many parse errors on UDP stream.")
                    break
                continue

            # ── Step 4: Clear error counter on a valid packet ──────────────
            
            # A single successful packet after a burst of bad ones resets the
            # counter. This prevents noise from accumulating across sessions.
            
            parse_errors = 0

            # ── Step 5: Feed the pipeline ──────────────────────────────────
            
            # ingest() is the first call — it stores data in the thread-safe
            # ring buffer BEFORE the signal fires, so by the time the GUI
            # timer reads a snapshot the sample is already in the buffer.
            
            self.data_handler.ingest(sample)
            
            # data_received is used by the main thread only to count bytes
            # (for the "133 Hz / 0.8 KB/s" status bar display). Even if the
            # ingestion gate is closed (before START), this fires so the LED
            # stays green and the byte counter advances.
            
            self.data_received.emit(sample)

    # ── Packet parser ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_packet(data: bytes) -> SensorSample | None:
        
        """
        Decode a 6-byte little-endian packet into a SensorSample.

        struct.unpack_from() is used instead of struct.unpack() so the call
        works on packets longer than PACKET_SIZE (e.g. if the firmware adds a
        sequence number byte in a future version) without raising an error.

        Division by SCALE_FACTOR (100.0) reverses the firmware's × 100 packing:
            int16 981  /  100.0  →  float  9.81 m/s²

        Returns None on struct error (malformed packet) so the caller can
        count it as a parse error rather than crash the thread.
        """
        try:
            ix, iy, iz = struct.unpack_from(PACKET_FORMAT, data, 0)
            ax = ix / SCALE_FACTOR   # m/s²
            ay = iy / SCALE_FACTOR   # m/s²
            az = iz / SCALE_FACTOR   # m/s²
            
            # time.time() is the Python wall clock — microsecond precision on
            # most platforms. We do NOT use a timestamp from the MCU because
            # the MCU clock is not synchronised to the PC clock, and any drift
            # or rollover would corrupt the elapsed-time axis on the plots.
            
            return SensorSample(time.time(), ax, ay, az)
        except (struct.error, TypeError):
            return None
