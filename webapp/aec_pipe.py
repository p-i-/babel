"""AECPipe — Python face of aec_helper (macOS system echo cancellation as a subprocess).

Replaces sounddevice for full-duplex voice apps: echo-cancelled 16 kHz int16 mic
arrives via a reader-thread callback; 24 kHz int16 playback is queued with .play()
and dropped instantly with .flush_playback() (barge-in). See aec_helper.swift for
the protocol; design + measurements: experiments/09-backend-aec/README.md.
(The server auto-builds the binary from aec_helper.swift at boot — see
ensure_aec_helper() in server.py.)
"""
import queue
import struct
import subprocess
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER = HERE / "aec_helper"
MIC_CHUNK = 3200          # 100 ms of 16 kHz int16 — matches the Live API send cadence


class AECPipe:
    def __init__(self, on_mic, aec=True, helper=HELPER, on_log=None, extra_args=None):
        """on_mic(bytes) is called from a reader THREAD with each 100 ms mic chunk.
        on_log(line) gets the helper's stderr lines (status + faults)."""
        helper = Path(helper)
        if not helper.exists():
            raise FileNotFoundError(f"{helper} missing — run build.sh first")
        args = [str(helper)] + ([] if aec else ["--no-aec"]) + (extra_args or [])
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._on_mic = on_mic
        self._on_log = on_log or (lambda line: None)
        self._wlock = threading.Lock()
        # Playback frames are written to the helper on a DEDICATED thread, never on
        # the caller's asyncio loop. The helper applies realtime backpressure (it
        # drains at 24 kHz), so a blocking stdin.write here would otherwise stall the
        # event loop for the length of a buffered turn — starving the WebSocket
        # handshake and everything else (field 2026-07-06: 'front-end unresponsive'
        # correlated exactly with audio flowing). The queue holds pending audio
        # off-loop instead.
        self._outq = queue.Queue()
        self.ready = threading.Event()          # set once the helper prints "ready"
        threading.Thread(target=self._read_mic, daemon=True).start()
        threading.Thread(target=self._read_log, daemon=True).start()
        threading.Thread(target=self._write_loop, daemon=True).start()

    def _read_mic(self):
        while True:
            data = self.proc.stdout.read(MIC_CHUNK)     # blocks for the full chunk
            if not data:
                return                                  # helper exited
            self._on_mic(data)

    def _read_log(self):
        for raw in self.proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if "ready" in line:
                self.ready.set()
            self._on_log(line)

    def _write_loop(self):
        # the ONLY place we block on stdin.write — off the caller's event loop
        while True:
            item = self._outq.get()
            if item is None:                            # close sentinel
                return
            ftype, payload = item
            self._raw_send(ftype, payload)

    def _raw_send(self, ftype, payload=b""):
        with self._wlock:
            if self.proc.stdin.closed:
                return
            try:
                self.proc.stdin.write(ftype + struct.pack("<I", len(payload)) + payload)
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass                                    # helper died; caller sees EOF

    def _drop_queued(self):
        try:
            while True:
                self._outq.get_nowait()
        except queue.Empty:
            pass

    def play(self, pcm_24k_int16):
        """Queue playback (non-blocking — enqueued and written on the writer
        thread). This audio becomes the AEC reference — the mic stream on stdout
        has it subtracted."""
        self._outq.put((b"A", pcm_24k_int16))

    def flush_playback(self):
        """Barge-in: drop the unwritten backlog immediately, then tell the helper
        to drop whatever it has already buffered."""
        self._drop_queued()
        self._outq.put((b"F", b""))

    def pause(self):
        """Stop the audio unit entirely — no mic, no playback, and macOS
        releases its voice-processing grip on the device (other apps' mic
        capture returns to full level). Queued playback is dropped."""
        self._drop_queued()
        self._outq.put((b"P", b""))

    def resume(self):
        """Start the audio unit again after pause()."""
        self._outq.put((b"R", b""))

    def close(self):
        self._drop_queued()
        self._outq.put(None)                            # stop the writer thread
        try:
            with self._wlock:
                self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()
