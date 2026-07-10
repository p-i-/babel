"""Raw session capture — the dumb, complete, live-path record.

Doctrine (2026-07-10, after the session-log redesign discussion): capture RAW,
derive views OFFLINE. The live path writes only two things, verbatim and
untruncated:

  * ws.jsonseq  — every WebSocket frame to/from the engine, in+out, exactly as it
                crossed the wire (mic + model audio ride along as base64 inside).
  * log.jsonseq — every log record the app emits.

Records are framed RFC-7464-style: each begins with an ASCII Record Separator
(0x1E) and ends with LF. RS is *provably* absent from the payload — JSON escapes
every control char, so a conformant frame can never contain a raw 0x1E — which
lets us store each frame BYTE-EXACT even though a frame may itself contain
newlines (Gemini pretty-prints incoming ones). Nothing here parses, compacts, or
mutates a frame: that would be interpretation, and interpretation lives OFFLINE.

Within a record: `<wall> <mono> <payload>` — BOTH clocks, stamped at the instant
of the call by the one `append()` choke point, so timestamps are consistent by
construction. wall = absolute + cross-process; mono = monotonic (correct deltas,
immune to NTP steps). Their divergence exposes a clock step instead of hiding it.

Read side (offline, disposable): `blob.split('\\x1e')`, drop the empty first
chunk, and for each record split the 3-token prefix (wall, mono, dir/level) off
the front — the remainder is the verbatim frame.

The only artifact we record that never touches the wire is `playback.wav` (the
actual speaker mix: model + clips + earcons + tail-clamp + flushes), written by
the server into this session's folder.
"""
import json
import logging
import threading
import time
from pathlib import Path

LOGS = Path(__file__).resolve().parent / "logs"
_COUNTER_LOCK = threading.Lock()
RS = "\x1e"                      # ASCII Record Separator — the record delimiter


def _text(data):
    """A frame as verbatim text — NO parsing/compacting, byte-exact. Frames are
    JSON text; bytes decode to the same. A (never-observed) non-UTF-8 binary
    frame is noted, not silently dropped."""
    if isinstance(data, (bytes, bytearray)):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary {len(data)} bytes>"
    return data


class Capture:
    """Owns one session's folder, the monotonic session counter, and the append
    choke point (single lock + line-buffered handle cache)."""

    def __init__(self, *, model, langs, temp, think, voice, argv):
        LOGS.mkdir(exist_ok=True)
        self.id = self._next_id()
        self.dir = LOGS / self.id
        self.dir.mkdir()
        self._lock = threading.Lock()
        self._handles = {}
        self._closed = False
        self.meta = {
            "session": self.id,
            "t0_wall": time.time(),          # absolute origin — recovers wall time
            "t0_mono": time.monotonic(),     # monotonic origin — for relative deltas
            "model": model, "langs": langs, "temp": temp, "think": think,
            "voice": voice, "argv": argv,
        }
        self._write_meta()

    def _write_meta(self):
        (self.dir / "meta.json").write_text(json.dumps(self.meta, indent=2, default=str))

    def record_config(self, config_obj):
        """Capture the full LiveConnectConfig handed to connect() — the setup
        frame (system prompt, tools, generation/thinking/VAD config) is sent
        INSIDE connect() before we can tee `_ws`, so it never hits ws.jsonseq.
        This is its deterministic source, at the boundary we control. Once only;
        never fatal."""
        if "config" in self.meta:
            return
        try:
            self.meta["config"] = config_obj.model_dump(mode="json", exclude_none=True)
        except Exception as e:
            self.meta["config"] = {"_error": f"model_dump failed: {e}",
                                   "_repr": str(config_obj)[:1000]}
        self._write_meta()

    @staticmethod
    def _next_id():
        cf = LOGS / ".counter"
        with _COUNTER_LOCK:
            n = 1
            if cf.exists():
                try:
                    n = int(cf.read_text().strip()) + 1
                except ValueError:
                    pass
            cf.write_text(str(n))
        return f"{n:06d}"

    def append(self, filename, payload):
        """One RS-framed record, dual-stamped, payload verbatim. Thread-safe
        (mic/log come off other threads). Line-buffered: always current for a
        live `tail`, nothing lost on kill, no flush logic."""
        line = f"{RS}{time.time():.6f} {time.monotonic():.6f} {payload}\n"
        with self._lock:
            if self._closed:                # never reopen (mode "w") → truncate after close
                return
            fh = self._handles.get(filename)
            if fh is None:
                fh = self._handles[filename] = open(
                    self.dir / filename, "w", buffering=1)
            fh.write(line)

    def path(self, filename):
        return self.dir / filename

    def close(self):
        with self._lock:
            self._closed = True
            for fh in self._handles.values():
                try:
                    fh.close()
                except Exception:
                    pass
            self._handles.clear()


class _WSTee:
    """Transparent wrapper over the SDK's websocket: tees every frame (in/out)
    verbatim to ws.jsonseq and forwards everything else untouched. Must be
    re-applied on EVERY (re)connect — a rotated session gets a fresh `_ws`."""

    def __init__(self, real, cap):
        self._real = real
        self._cap = cap

    async def send(self, data, *a, **k):
        self._cap.append("ws.jsonseq", "out " + _text(data))
        return await self._real.send(data, *a, **k)

    async def recv(self, *a, **k):
        data = await self._real.recv(*a, **k)
        self._cap.append("ws.jsonseq", "in " + _text(data))
        return data

    def __getattr__(self, name):        # close, state, close_code, … → real ws
        return getattr(self._real, name)


def wrap_ws(session, cap):
    """Tee `session._ws`. Idempotent, and a no-op if the shape ever changes
    (capture must never take down a session)."""
    ws = getattr(session, "_ws", None)
    if ws is not None and not isinstance(ws, _WSTee):
        session._ws = _WSTee(ws, cap)


class CaptureLogHandler(logging.Handler):
    """Route every log record into log.jsonseq, untruncated."""

    def __init__(self, cap):
        super().__init__()
        self._cap = cap

    def emit(self, r):
        try:
            self._cap.append("log.jsonseq", json.dumps(
                {"lvl": r.levelname, "logger": r.name, "msg": r.getMessage()},
                ensure_ascii=False, default=str))
        except Exception:
            pass
