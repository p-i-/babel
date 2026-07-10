"""
babel webapp — the squeaky-clean core.

    a voice, a stage, an eye, a memory, Python, and one verb.

CORE (code): the live audio loop · shell chrome with control buttons · a stage iframe
the teacher populates · /clip + /speak audio services · ONE tool: run_python.
CONTRACT: feed() — "print for things" — in both Python and stage JS; everything the
teacher perceives (feeds, job completions, UI events, student actions) arrives on ONE
event stream, delivered when the teacher is idle.
AUDIO: all I/O rides aec_helper (macOS Voice-Processing I/O as a subprocess —
experiments/09): the mic stays OPEN while the teacher speaks, echo is cancelled at
the OS, and the student interrupts BY VOICE (server VAD `interrupted` → flush).
The old half-duplex echo gate is gone; mic is blocked only by mute/suspend/PTT.
TEACHER-OWNED: workspace/ — its persistent home. helpers.py is seeded once, then hers.
PROMPT: repo-root system.md.

Run (reads keys from repo-root .env):
    ./.venv/bin/python webapp/server.py [--ptt] [--voice NAME] [--langs en,uk]
                                        [--port N] [--verbose]
    ./.venv/bin/python webapp/server.py --selftest    # E2E: model calls run_python
Then open http://127.0.0.1:8642
"""
import array
import ast
import asyncio
import hashlib
import json
import os
import logging
import math
import shutil
import signal
import struct
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path

from aiohttp import web, WSMsgType
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from websockets.exceptions import ConnectionClosed

from capture import Capture, wrap_ws, CaptureLogHandler

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[0]
load_dotenv(ROOT / ".env")

MODEL = "gemini-3.1-flash-live-preview"
PORT = 8642
IN_RATE, OUT_RATE = 16000, 24000
TAIL_GRACE_S = 1.5             # queue up to this much CONTIGUOUS near-silent model audio
                               # (natural pauses/pacing survive — exp-11) then clamp the rest
                               # (the model's stochastic room-tone tail — exp-13)
MAX_RECONNECT = 5              # consecutive failures before we start shouting (never give up)
HEARTBEAT_S = 30               # exp-07: idle kill at ~152s / audio gap kill at ~50s;
SILENCE_100MS = b"\x00" * (IN_RATE // 10 * 2)   # a 30s silence pulse (~5 tok/min) prevents both
DEFAULT_VOICE = "Sulafat"      # tutor voice
CLIP_VOICE = "Charon"          # native-speaker clip voice


def _earcon_pcm(segments, rate=OUT_RATE):
    """Synthesize a short earcon as 24 kHz int16 mono little-endian PCM. Each
    segment is (freq_hz, dur_s, gain, delay_s), summed with a decay envelope.
    Precomputed once at startup; played through the AEC output path so it's
    echo-cancelled (can't trip VAD), self-records into tutor.wav, and is logged."""
    total = max((delay + dur for (_f, dur, _g, delay) in segments), default=0.0)
    n = int(total * rate)
    buf = [0.0] * n
    for freq, dur, gain, delay in segments:
        i0 = int(delay * rate)
        for k in range(int(dur * rate)):
            if i0 + k >= n:
                break
            t = k / rate
            env = math.exp(-t / (dur * 0.4 + 1e-6))
            buf[i0 + k] += gain * env * math.sin(2 * math.pi * freq * t)
    a = array.array("h", (max(-32767, min(32767, int(v * 32767))) for v in buf))
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


# vocabulary: cue the consequential, near-silent on the normal; failure is loudest
EARCON_SPECS = {
    "reach": [(660, 0.05, 0.25, 0.0)],                             # tool invoked
    "ok":    [(880, 0.06, 0.20, 0.0)],                             # ran clean
    "async": [(700, 0.05, 0.20, 0.0), (700, 0.05, 0.20, 0.09)],   # went background
    "fail":  [(185, 0.24, 0.55, 0.0), (140, 0.24, 0.55, 0.05)],   # FAILURE — low buzz
    "barge": [(540, 0.09, 0.30, 0.0), (330, 0.08, 0.30, 0.05)],   # you barged in
    "link":  [(440, 0.10, 0.25, 0.0), (660, 0.11, 0.25, 0.10)],   # connection event
}
EARCONS = {name: _earcon_pcm(spec) for name, spec in EARCON_SPECS.items()}


def _earcon_wav(path, gain=1.0):
    """Load a REAL recorded earcon (24 kHz mono 16-bit PCM WAV) as raw PCM bytes
    for the AEC output path — same lane as the synth earcons above. Returns b''
    if the file is missing or the wrong format (never fatal — the cue just goes
    silent)."""
    import wave as _wave
    try:
        with _wave.open(str(path), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (OUT_RATE, 1, 2):
                return b""
            pcm = w.readframes(w.getnframes())
    except Exception:
        return b""
    if gain != 1.0 and pcm:
        a = array.array("h"); a.frombytes(pcm)
        for i in range(len(a)):
            a[i] = max(-32767, min(32767, int(a[i] * gain)))
        if sys.byteorder == "big":
            a.byteswap()
        pcm = a.tobytes()
    return pcm


# the camera-shutter cue for peek() — a real recorded shutter (grabbed 2026-07-08),
# attenuated so it sits under the tutor's voice rather than startling the student
_shutter = _earcon_wav(HERE / "assets" / "camera-shutter_24k_mono.wav", gain=0.5)
if _shutter:
    EARCONS["shutter"] = _shutter
JOB_TIMEOUT = 30               # SIGTERM at 30s, SIGKILL +3s
JOB_GRACE = 1.2                # tool response waits this long for quick jobs
FEED_MAX_IMAGES = 4            # images per delivered batch
FEED_MAX_TEXT = 6000           # chars per delivered batch
WORKSPACE = HERE / "workspace"
CLIPS_DIR = HERE / "clips"

# ── on-screen keyboard: rows of [physical event.code, glyph] (exp-05 showcase).
# Pure MECHANISM: the widget renders keys and colors them as told; what colors
# MEAN is the teacher's prompt-level business (single dumb primitive principle).
KBD_LAYOUTS = {
    "uk": [
        [["KeyQ", "й"], ["KeyW", "ц"], ["KeyE", "у"], ["KeyR", "к"], ["KeyT", "е"],
         ["KeyY", "н"], ["KeyU", "г"], ["KeyI", "ш"], ["KeyO", "щ"], ["KeyP", "з"],
         ["BracketLeft", "х"], ["BracketRight", "ї"]],
        [["KeyA", "ф"], ["KeyS", "і"], ["KeyD", "в"], ["KeyF", "а"], ["KeyG", "п"],
         ["KeyH", "р"], ["KeyJ", "о"], ["KeyK", "л"], ["KeyL", "д"],
         ["Semicolon", "ж"], ["Quote", "є"]],
        [["KeyZ", "я"], ["KeyX", "ч"], ["KeyC", "с"], ["KeyV", "м"], ["KeyB", "и"],
         ["KeyN", "т"], ["KeyM", "ь"], ["Comma", "б"], ["Period", "ю"],
         ["Backslash", "ґ"]],
    ],
    "en": [
        [["KeyQ", "q"], ["KeyW", "w"], ["KeyE", "e"], ["KeyR", "r"], ["KeyT", "t"],
         ["KeyY", "y"], ["KeyU", "u"], ["KeyI", "i"], ["KeyO", "o"], ["KeyP", "p"]],
        [["KeyA", "a"], ["KeyS", "s"], ["KeyD", "d"], ["KeyF", "f"], ["KeyG", "g"],
         ["KeyH", "h"], ["KeyJ", "j"], ["KeyK", "k"], ["KeyL", "l"]],
        [["KeyZ", "z"], ["KeyX", "x"], ["KeyC", "c"], ["KeyV", "v"], ["KeyB", "b"],
         ["KeyN", "n"], ["KeyM", "m"]],
    ],
    "ru": [
        [["KeyQ", "й"], ["KeyW", "ц"], ["KeyE", "у"], ["KeyR", "к"], ["KeyT", "е"],
         ["KeyY", "н"], ["KeyU", "г"], ["KeyI", "ш"], ["KeyO", "щ"], ["KeyP", "з"],
         ["BracketLeft", "х"], ["BracketRight", "ъ"]],
        [["KeyA", "ф"], ["KeyS", "ы"], ["KeyD", "в"], ["KeyF", "а"], ["KeyG", "п"],
         ["KeyH", "р"], ["KeyJ", "о"], ["KeyK", "л"], ["KeyL", "д"],
         ["Semicolon", "ж"], ["Quote", "э"], ["Backquote", "ё"]],
        [["KeyZ", "я"], ["KeyX", "ч"], ["KeyC", "с"], ["KeyV", "м"], ["KeyB", "и"],
         ["KeyN", "т"], ["KeyM", "ь"], ["Comma", "б"], ["Period", "ю"]],
    ],
    "el": [
        [["KeyW", "ς"], ["KeyE", "ε"], ["KeyR", "ρ"], ["KeyT", "τ"], ["KeyY", "υ"],
         ["KeyU", "θ"], ["KeyI", "ι"], ["KeyO", "ο"], ["KeyP", "π"]],
        [["KeyA", "α"], ["KeyS", "σ"], ["KeyD", "δ"], ["KeyF", "φ"], ["KeyG", "γ"],
         ["KeyH", "η"], ["KeyJ", "ξ"], ["KeyK", "κ"], ["KeyL", "λ"]],
        [["KeyZ", "ζ"], ["KeyX", "χ"], ["KeyC", "ψ"], ["KeyV", "ω"], ["KeyB", "β"],
         ["KeyN", "ν"], ["KeyM", "μ"]],
    ],
    "de": [
        [["KeyQ", "q"], ["KeyW", "w"], ["KeyE", "e"], ["KeyR", "r"], ["KeyT", "t"],
         ["KeyY", "z"], ["KeyU", "u"], ["KeyI", "i"], ["KeyO", "o"], ["KeyP", "p"],
         ["BracketLeft", "ü"], ["Minus", "ß"]],
        [["KeyA", "a"], ["KeyS", "s"], ["KeyD", "d"], ["KeyF", "f"], ["KeyG", "g"],
         ["KeyH", "h"], ["KeyJ", "j"], ["KeyK", "k"], ["KeyL", "l"],
         ["Semicolon", "ö"], ["Quote", "ä"]],
        [["KeyZ", "y"], ["KeyX", "x"], ["KeyC", "c"], ["KeyV", "v"], ["KeyB", "b"],
         ["KeyN", "n"], ["KeyM", "m"]],
    ],
    "fr": [
        [["Digit2", "é"], ["Digit7", "è"], ["Digit9", "ç"], ["Digit0", "à"],
         ["Quote", "ù"]],
        [["KeyQ", "a"], ["KeyW", "z"], ["KeyE", "e"], ["KeyR", "r"], ["KeyT", "t"],
         ["KeyY", "y"], ["KeyU", "u"], ["KeyI", "i"], ["KeyO", "o"], ["KeyP", "p"]],
        [["KeyA", "q"], ["KeyS", "s"], ["KeyD", "d"], ["KeyF", "f"], ["KeyG", "g"],
         ["KeyH", "h"], ["KeyJ", "j"], ["KeyK", "k"], ["KeyL", "l"],
         ["Semicolon", "m"]],
        [["KeyZ", "w"], ["KeyX", "x"], ["KeyC", "c"], ["KeyV", "v"], ["KeyB", "b"],
         ["KeyN", "n"]],
    ],
    "es": [
        [["KeyQ", "q"], ["KeyW", "w"], ["KeyE", "e"], ["KeyR", "r"], ["KeyT", "t"],
         ["KeyY", "y"], ["KeyU", "u"], ["KeyI", "i"], ["KeyO", "o"], ["KeyP", "p"]],
        [["KeyA", "a"], ["KeyS", "s"], ["KeyD", "d"], ["KeyF", "f"], ["KeyG", "g"],
         ["KeyH", "h"], ["KeyJ", "j"], ["KeyK", "k"], ["KeyL", "l"],
         ["Semicolon", "ñ"]],
        [["KeyZ", "z"], ["KeyX", "x"], ["KeyC", "c"], ["KeyV", "v"], ["KeyB", "b"],
         ["KeyN", "n"], ["KeyM", "m"]],
    ],
    "it": [
        [["KeyQ", "q"], ["KeyW", "w"], ["KeyE", "e"], ["KeyR", "r"], ["KeyT", "t"],
         ["KeyY", "y"], ["KeyU", "u"], ["KeyI", "i"], ["KeyO", "o"], ["KeyP", "p"],
         ["BracketLeft", "è"], ["Equal", "ì"]],
        [["KeyA", "a"], ["KeyS", "s"], ["KeyD", "d"], ["KeyF", "f"], ["KeyG", "g"],
         ["KeyH", "h"], ["KeyJ", "j"], ["KeyK", "k"], ["KeyL", "l"],
         ["Semicolon", "ò"], ["Quote", "à"], ["Backslash", "ù"]],
        [["KeyZ", "z"], ["KeyX", "x"], ["KeyC", "c"], ["KeyV", "v"], ["KeyB", "b"],
         ["KeyN", "n"], ["KeyM", "m"]],
    ],
    "tr": [
        [["KeyQ", "q"], ["KeyW", "w"], ["KeyE", "e"], ["KeyR", "r"], ["KeyT", "t"],
         ["KeyY", "y"], ["KeyU", "u"], ["KeyI", "ı"], ["KeyO", "o"], ["KeyP", "p"],
         ["BracketLeft", "ğ"], ["BracketRight", "ü"]],
        [["KeyA", "a"], ["KeyS", "s"], ["KeyD", "d"], ["KeyF", "f"], ["KeyG", "g"],
         ["KeyH", "h"], ["KeyJ", "j"], ["KeyK", "k"], ["KeyL", "l"],
         ["Semicolon", "ş"], ["Quote", "i"]],
        [["KeyZ", "z"], ["KeyX", "x"], ["KeyC", "c"], ["KeyV", "v"], ["KeyB", "b"],
         ["KeyN", "n"], ["KeyM", "m"], ["Comma", "ö"], ["Period", "ç"]],
    ],
}
# NOT covered (deliberately, single-layer widget): RTL scripts (he/ar), AltGr-heavy
# layouts (pl), CJK/IME — see ROADMAP T3.


def pick_kbd_lang(langs):
    """The TARGET language's layout (convention: --langs native,target → last wins)."""
    for lang in reversed(langs or []):
        if lang in KBD_LAYOUTS:
            return lang
    return "en"


TTS_SYSTEM = """You are a text-to-speech engine, not an assistant.
The user sends text. You speak that text aloud, EXACTLY as written, and NOTHING else.
Never greet, never comment, never confirm, never translate, never add or repeat words.
If the text is a single word, speak just that single word.
Speak clearly at a natural pace, as a native speaker of the text's language."""

log = logging.getLogger("webapp")


def spawn(coro, name):
    """create_task that can never fail silently: any non-cancelled exception is
    logged the moment the task dies (asyncio's own 'exception was never retrieved'
    fires only at GC time, if ever — that's how async failures go unrecorded)."""
    task = asyncio.create_task(coro, name=name)

    def _report(t):
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("task %s crashed: %r", name, exc, exc_info=exc)
    task.add_done_callback(_report)
    return task


def is_conn_closed(exc):
    """The server closing the websocket is an expected, recoverable event
    (exp-07: idle kill / 10-min rotation) — not an application error."""
    return isinstance(exc, (ConnectionClosed, genai_errors.APIError))


class ChatLogHandler(logging.Handler):
    """Mirror this app's WARNING/ERROR records into the browser chat rail:
    anything worth logging loudly is worth showing the user (⚠️/❌)."""
    def __init__(self, buf):
        super().__init__(level=logging.WARNING)
        self.buf = buf

    def emit(self, record):
        try:
            self.buf.append((record.levelname, record.getMessage()))
            del self.buf[:-50]                     # keep the newest 50
        except Exception:                          # never let display kill logging
            pass


def pcm_dbfs(pcm):
    """RMS dBFS of int16 PCM (cheap enough for 100ms chunks at 10Hz)."""
    n = len(pcm) // 2
    if n == 0:
        return -120.0
    vals = struct.unpack(f"<{n}h", pcm)
    rms = math.sqrt(sum(v * v for v in vals) / n)
    return 20 * math.log10(max(rms, 1.0) / 32768.0)


def cli_arg(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def parse_langs():
    raw = cli_arg("--langs", "")
    return [s.strip() for s in raw.split(",") if s.strip()] or None


# ── THE tool ─────────────────────────────────────────────────────────────────
RUN_PYTHON = types.FunctionDeclaration(
    name="run_python",
    description=(
        "Run Python code in YOUR persistent workspace (cwd). Your single tool — "
        "everything else happens through it via helpers.py (already in your workspace: "
        "show/show_html/show_text for the student's stage — ONE surface you DESIGN "
        "as HTML, each show replaces the last — warm for click-to-hear clips, "
        "peek to SEE the student's screen, feed for sending yourself images/annotated "
        "text, run_js to inspect or patch the live stage page, "
        "search_images/contact_sheet/vision_pick "
        "for web images). print() comes back in the receipt; feed() items arrive as a "
        "follow-up message (images included). Quick code returns its result "
        "immediately; longer code returns a job id and reports back by itself when "
        "done — never wait for it, never fill the silence. 30s limit per job."),
    parameters=types.Schema(type="OBJECT", properties={
        "code": types.Schema(type="STRING",
                             description="Python source. Use `from helpers import *`."),
        "purpose": types.Schema(type="STRING",
                                description="One short line: why (echoed back with the results)."),
    }, required=["code"]),
)

# The "no-op" tool. Your architecture FORCES you to emit audio for every turn, so when a
# turn should be silent (the student is thinking out loud, talking to someone in the room,
# etc.) you can't just say nothing. Call this and whatever audio you produce THIS turn is
# scrubbed — the student hears silence. Say only a tiny marker (e.g. "one moment"); it won't
# reach them.
STAY_SILENT = types.FunctionDeclaration(
    name="stay_silent",
    description=(
        "Take a SILENT turn: whatever audio you emit this turn is scrubbed and the student "
        "hears nothing. Use it when a response from you is NOT wanted right now — the student "
        "is thinking out loud, mid-word, talking to someone else in the room, or reacting to "
        "something off-app — and you'd otherwise be forced to interrupt with speech. Keep any "
        "spoken audio to a word or two; it will not be played."),
    parameters=types.Schema(type="OBJECT", properties={
        "reason": types.Schema(type="STRING",
                               description="One short line: why staying silent (for the log)."),
    }),
)


def helpers_reference():
    """The live single source of truth: signatures + docstrings extracted from the
    TEACHER'S OWN workspace/helpers.py (ast — never imported/executed). The prompt's
    tool briefing therefore always matches the file, including the teacher's own
    edits. Field motivation: a helper's call signature lived only in its docstring,
    and the teacher re-guessed it wrong in three separate sessions."""
    try:
        tree = ast.parse((WORKSPACE / "helpers.py").read_text())
    except (OSError, SyntaxError) as e:
        # a broken helpers.py is the teacher's to fix — tell it, loudly, in-brief
        return (f"(!! your helpers.py could not be parsed: {e} — it is broken; "
                f"read it and repair it before relying on any helper !!)")
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            doc = ast.get_docstring(node) or "(no docstring)"
            body = "\n".join("    " + ln for ln in doc.splitlines())
            chunks.append(f"{node.name}({ast.unparse(node.args)})\n{body}")
    return "\n".join(chunks)


def build_config(ptt, resume_handle, voice, langs, temp=0.0, think="high",
                 thoughts=True):
    import os as _os
    prompt = (ROOT / "system.md").read_text().strip()
    prompt = prompt.replace("{{WORKSPACE_HELPERS}}", helpers_reference())
    # current wall-clock, re-substituted on every connect/resume so spacing reasoning
    # (how long since the last class / recall) has a real anchor. See THINK LIKE A TEACHER.
    prompt = prompt.replace("{{NOW}}", time.strftime("%A %Y-%m-%d %H:%M %Z"))
    if _os.environ.get("DEV_MODE", "").lower() in ("dev", "1", "true"):
        prompt += ("\n\nDEVELOPER MODE (DEV_MODE=dev)\n"
                   "The 'student' is your creator, field-testing you. YOUR PRIMARY "
                   "ROLE HERE IS IMPROVING THE TECHNOLOGY; the lesson is roleplay, "
                   "run to exercise the machinery and check your performance. Teach "
                   "it genuinely and well — but the moment the user escalates (asks "
                   "about your internals, reports a glitch, says 'let's debug', or "
                   "otherwise steps outside the lesson frame), SWITCH FULLY to "
                   "engineering-collaborator mode: investigate, run diagnostics, "
                   "report plainly, leave evidence files (show paths). Do NOT steer "
                   "back to the lesson — the user decides when the roleplay resumes. "
                   "Operational anomalies OUTRANK the teaching goal at all times; "
                   "never paper over a glitch to keep the lesson smooth. Transparency "
                   "of your internal state is the primary product in this mode.")
    in_tr = (types.AudioTranscriptionConfig(
                 language_hints=types.LanguageHints(language_codes=langs))
             if langs else types.AudioTranscriptionConfig())
    kwargs = dict(
        response_modalities=["AUDIO"],
        temperature=temp,                       # --temp (default 0.0); see run()
        system_instruction=prompt,
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
        input_audio_transcription=in_tr,
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # Layer 1 (2026-07-06): thinkingLevel is a per-connection ceiling
        # (minimal|low|medium|high). 3.1 modulates spend to the task, so `high`
        # is not a flat latency tax — measure, don't assume (exp/roadmap T5b: a
        # config win here may obviate the parked split-brain planner). Pairs with
        # temp=0. include_thoughts streams reasoning text as `thought` parts
        # (surfaced live in the rail + logged) — our developer-facing eye on WHY.
        thinking_config=types.ThinkingConfig(
            thinking_level=think, include_thoughts=thoughts),
        tools=[types.Tool(function_declarations=[RUN_PYTHON, STAY_SILENT])],
        # Compression thresholds (exp-08 + field 2026-07-04): the real window is
        # ≥38k (the old 6000/4000 was set on a dead 8192-cap theory). CRITICAL
        # CONSTRAINT (probed, probe_compression_floor.py): a compression cycle
        # whose target can't fit the UNCOMPRESSIBLE prefix (system prompt ~3.5k
        # and growing with {{WORKSPACE_HELPERS}}) kills the connection with
        # "1007 invalid argument". Keep target ≫ prompt size, trigger ≫ target:
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=24000,
            sliding_window=types.SlidingWindow(target_tokens=12000)),
        session_resumption=types.SessionResumptionConfig(handle=resume_handle),
    )
    if ptt:
        kwargs["realtime_input_config"] = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True))
    return types.LiveConnectConfig(**kwargs)


def setup_logging(cap, verbose):
    """Every log record → log.jsonseq (untruncated, dual-stamped). log.jsonseq IS the
    log now — no separate text file. --verbose also mirrors to the console."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(CaptureLogHandler(cap))
    if verbose:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s", datefmt="%H:%M:%S"))
        root.addHandler(console)
    lvl = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("websockets").setLevel(lvl)
    logging.getLogger("google_genai").setLevel(lvl)
    logging.getLogger("aiohttp").setLevel(logging.INFO)


# ── job execution (module level: selftest + unit-testable) ──────────────────
async def execute_job(code, workspace, port, timeout=JOB_TIMEOUT):
    """Run code in a subprocess. Returns (rc, stdout, stderr, feeds[])."""
    import os as _os
    jobs_dir = workspace / ".jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    feed_file = jobs_dir / f"feed_{int(time.time()*1e6)}.jsonl"
    env = dict(_os.environ)
    env.update({"WORKSPACE": str(workspace), "FEED_FILE": str(feed_file),
                "SERVER_PORT": str(port), "PYTHONPATH": str(workspace)})
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, cwd=str(workspace), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
        rc = -1
        err = (err or b"") + f"\n[TIMEOUT: killed after {timeout}s]".encode()
    feeds = []
    if feed_file.exists():
        for line in feed_file.read_text().splitlines():
            try:
                feeds.append(json.loads(line))
            except ValueError:
                pass
        feed_file.unlink()
    return rc, out.decode(errors="replace"), err.decode(errors="replace"), feeds


def trunc(s, n=2000):
    return s if len(s) <= n else s[:n] + f"\n…[truncated, {len(s)} chars total]"


STDOUT_MODEL_CAP = 8000     # model sees head+pointer past this (protects the window
                            # from a runaway print); the FULL copy is saved to .jobs
STDERR_MODEL_CAP = 20000    # errors are never legitimately huge — effectively uncapped


def stash_job_output(jid, out, err):
    """Persist the FULL stdout/stderr to .jobs/ and return (out, err) for the model.
    Nothing is silently lost: errors reach the model WHOLE (a traceback is never
    legitimately gigantic), and stdout is capped but RECOVERABLE — the model (and
    future-you reading the log) gets the head plus a workspace path to the full
    output, readable with run_python. Replaces the old blind trunc() that starved
    both the model and the log (field 2026-07-06: the teacher's own notes read got
    chopped at 2000 chars, cutting the recent session_log)."""
    jdir = WORKSPACE / ".jobs"

    def keep(kind, text, cap):
        if not text or len(text) <= cap:
            return text
        try:
            jdir.mkdir(parents=True, exist_ok=True)
            path = jdir / f"job_{jid}.{kind}.txt"
            path.write_text(text)
            rel = path.relative_to(WORKSPACE)
            return (text[:cap] + f"\n…[+{len(text) - cap} more chars; FULL {kind} "
                    f"saved to {rel} — read it with run_python if you need the rest]")
        except OSError:
            return text[:cap] + f"\n…[truncated, {len(text)} chars total]"

    return keep("out", out, STDOUT_MODEL_CAP), keep("err", err, STDERR_MODEL_CAP)


def one_line_error(stderr):
    """Hoist the actionable line out of a Python traceback for the failure
    receipt/⚡ event — the last non-blank line is the exception itself (e.g.
    'RuntimeError: /keyboard failed (400): ... no such key(s): [i] ...'),
    which is what the teacher must actually read to self-correct."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return trunc(lines[-1], 300) if lines else ""


def ensure_built(name):
    """Build a Swift helper binary if missing or its source is newer (one swiftc
    call, ~2s). Fail LOUDLY — the helpers are load-bearing."""
    src = HERE / f"{name}.swift"
    binp = HERE / name
    if binp.exists() and binp.stat().st_mtime >= src.stat().st_mtime:
        return binp
    import subprocess
    print(f"● building {name} (first run / source changed)…")
    r = subprocess.run(["swiftc", "-O", "-o", str(binp), str(src)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{name} build failed (need Xcode CLT / swiftc):\n"
                           f"{r.stderr[:2000]}")
    return binp


def ensure_aec_helper():
    return ensure_built("aec_helper")


def get_input_volume():
    """The SYSTEM input gain — a device-global setting VPIO's AGC used to walk
    down (breaking other mic apps). AGC is now off in the helper; this guard
    verifies and repairs at teardown anyway (belt and braces)."""
    import subprocess
    try:
        r = subprocess.run(["osascript", "-e",
                            "input volume of (get volume settings)"],
                           capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip())
    except Exception:
        return None


def set_input_volume(v):
    import subprocess
    try:
        subprocess.run(["osascript", "-e", f"set volume input volume {v}"],
                       timeout=5)
        return True
    except Exception:
        return False


def read_kbd_legends():
    """The user's PHYSICAL keyboard: form factor (ansi/iso/jis) + keycap legends,
    from kbd_legends (one-shot, ~50ms) against the ACTIVE macOS input source.
    Fresh every boot — the input source is mutable state; caching it would
    eventually describe yesterday's keyboard. Legends are cosmetic: failure logs
    loudly but never blocks a lesson."""
    import subprocess
    try:
        r = subprocess.run([str(ensure_built("kbd_legends"))],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or f"rc={r.returncode}")
        d = json.loads(r.stdout)
        return (d.get("source", "?"), d.get("physical", "ansi"),
                d.get("legends", {}))
    except Exception as e:
        log.error("kbd_legends failed (%s) — keyboard shows no physical-key "
                  "labels this session", e)
        return "?", "ansi", {}


def seed_workspace():
    WORKSPACE.mkdir(exist_ok=True)
    helpers = WORKSPACE / "helpers.py"
    if not helpers.exists():                     # seed once; never overwrite
        shutil.copy(HERE / "seed_helpers.py", helpers)


def workspace_listing(max_entries=40):
    lines = []
    for p in sorted(WORKSPACE.rglob("*")):
        rel = p.relative_to(WORKSPACE)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if len(lines) >= max_entries:
            lines.append("…")
            break
        lines.append(str(rel) + ("/" if p.is_dir() else f" ({p.stat().st_size}B)"))
    return "\n".join(lines) or "(empty — this is a brand-new student)"


def session_gap():
    stamp = WORKSPACE / ".last_session"
    now = time.time()
    gap = None
    if stamp.exists():
        try:
            gap = now - float(stamp.read_text().strip())
        except ValueError:
            pass
    stamp.write_text(str(now))
    if gap is None:
        return "this is the FIRST session ever"
    if gap < 3600:
        return f"{gap/60:.0f} minutes since last session"
    if gap < 172800:
        return f"{gap/3600:.1f} hours since last session"
    return f"{gap/86400:.1f} days since last session (expect forgetting — recap first)"


# ── clip cache (Gemini-as-TTS; experiments/04) ───────────────────────────────
_clip_lock = asyncio.Lock()


def _norm(s):
    return " ".join("".join(c for c in s.lower() if c.isalnum() or c.isspace()).split())


def clip_path(text, voice, lang=None):
    # include lang in the cache key so cross-language homographs (FR/EN "chat")
    # cache separately; lang=None keeps the OLD key so existing clips still hit.
    key = f"{voice}|{lang}|{text}" if lang else f"{voice}|{text}"
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return CLIPS_DIR / f"{voice}_{h}.wav"


async def _generate_clip(client, text, voice, lang=None):
    sysi = TTS_SYSTEM + (f"\nThe text is in {lang}; pronounce it as a native "
                         f"{lang} speaker." if lang else "")
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"], system_instruction=sysi,
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
        output_audio_transcription=types.AudioTranscriptionConfig())
    pcm, said = bytearray(), []
    async with client.aio.live.connect(model=MODEL, config=cfg) as session:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=True)
        async for msg in session.receive():
            sc = msg.server_content
            if not sc:
                continue
            if sc.model_turn:
                for part in sc.model_turn.parts or []:
                    if part.inline_data and part.inline_data.data:
                        pcm.extend(part.inline_data.data)
            if sc.output_transcription and sc.output_transcription.text:
                said.append(sc.output_transcription.text)
    return bytes(pcm), "".join(said).strip()


async def ensure_clip(client, text, voice=CLIP_VOICE, lang=None):
    path = clip_path(text, voice, lang)
    if path.exists():
        return path
    async with _clip_lock:
        if path.exists():
            return path
        CLIPS_DIR.mkdir(exist_ok=True)
        pcm, said = await _generate_clip(client, text, voice, lang)
        want = _norm(text.strip(" .!?…"))
        if _norm(said.strip(" .!?…")) != want:
            log.warning("clip mismatch (want=%r said=%r) — retrying", text, said)
            pcm, said = await _generate_clip(client, text, voice, lang)
            if _norm(said.strip(" .!?…")) != want:
                # fail fast: NEVER cache a wrong pronunciation (a silent bad
                # clip would replay forever) — the caller gets the error
                log.error("clip mismatch twice (want=%r said=%r) — refusing "
                          "to cache", text, said)
                raise RuntimeError(f"TTS said {said!r} instead of {text!r} "
                                   f"(twice) — clip not cached")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(OUT_RATE)
            w.writeframes(pcm)
        log.info("clip %r voice=%s (%.1fs)", text, voice, len(pcm) / 2 / OUT_RATE)
    return path


# ── the bridge injected into every staged HTML page ──────────────────────────
BRIDGE = """<script>
(function(){
  const post = (m) => parent.postMessage(m, "*");
  window.feed = (payload, opts) => post({babel:"feed", payload,
                     engage: !(opts && opts.engage === false)});
  window.speak = (text, lang, voice) => post({babel:"speak", text, lang, voice});
  window.babel = {feed: window.feed, speak: window.speak, report: window.feed};
  /* a page bug must never die silently in the iframe console — the teacher
     wrote this page and is the only one who can fix it */
  window.addEventListener("error", (e) => post({babel:"jserror",
    text: `${e.message} @ line ${e.lineno}:${e.colno}`}));
  window.addEventListener("unhandledrejection", (e) => post({babel:"jserror",
    text: "unhandled promise rejection: " + String(e.reason)}));
  window.addEventListener("message", async (e) => {
    const d = e.data;
    if (!d || d.babel !== "exec") return;
    let out;
    try { out = {ok: true, result: await (new Function(d.code))()}; }
    catch (err) { out = {ok: false, error: String(err)}; }
    post({babel:"exec_result", id: d.id, ...out});
  });
})();
</script>"""

PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#111;color:#3a3a42;font:22px -apple-system,Helvetica,sans-serif}
</style></head><body>the teacher controls this space</body></html>"""


async def selftest():
    """E2E: model calls run_python for real; we execute and it speaks the answer."""
    seed_workspace()
    client = genai.Client()
    cfg = build_config("--ptt" in sys.argv, None, cli_arg("--voice", DEFAULT_VOICE),
                       parse_langs())
    async with client.aio.live.connect(model=MODEL, config=cfg) as session:
        print("connected — single-tool config accepted.")
        await session.send_client_content(turns=types.Content(role="user", parts=[
            types.Part(text="[SELFTEST] Call run_python with this EXACT code: "
                            "print(6*7)\nThen say the number it printed out loud "
                            "in YOUR OWN voice. NOTE: this is a plumbing test — "
                            "the app's HTTP services (show/show_html/warm/"
                            "set_keys) are OFFLINE and will fail; do not call "
                            "them.")]), turn_complete=True)
        said, called = [], False
        for _ in range(4):                      # a few receive passes (per-turn iterator)
            async for msg in session.receive():
                if msg.tool_call:
                    for fc in msg.tool_call.function_calls:
                        called = True
                        rc, out, err, feeds = await execute_job(
                            fc.args.get("code", ""), WORKSPACE, PORT)
                        print(f"tool_call executed: rc={rc} stdout={out.strip()!r}")
                        await session.send_tool_response(function_responses=[
                            types.FunctionResponse(id=fc.id, name=fc.name,
                                response={"status": "done", "exit_code": rc,
                                          "stdout": out, "stderr": err})])
                sc = msg.server_content
                if sc and sc.output_transcription and sc.output_transcription.text:
                    said.append(sc.output_transcription.text)
            if called and said:
                break
        text = "".join(said)
        print(f"model said: {text.strip()!r}")
        assert called, "model never called run_python"
        norm = text.lower().replace("-", " ")   # transcripts often spell numbers out
        assert "42" in text or "forty two" in norm, \
            "expected 42/forty-two in the spoken reply"
    print("SELFTEST OK")


async def run():
    from aec_pipe import AECPipe

    ptt = "--ptt" in sys.argv
    voice = cli_arg("--voice", DEFAULT_VOICE)
    langs = parse_langs()
    port = int(cli_arg("--port", str(PORT)))
    temp = float(cli_arg("--temp", "0.5"))      # 0.5 (was 0.0): temp<0.5 triggers the Live
                                                # "comfort-noise tail" stall (exp-14, forum
                                                # 174126). 0.5 is the knee: tail-free yet as
                                                # near-deterministic as we can get for tools.
    think = cli_arg("--think", "medium")        # thinkingLevel: minimal|low|medium|high
                                                # (dropped high→medium 2026-07-07: high
                                                # gave no greeting + long silent tails)
    thoughts = "--no-thoughts" not in sys.argv  # stream the agent's reasoning (default on)
    earcons_on = "--no-earcons" not in sys.argv  # audio cues for engine state (default on)
    dev_mode = os.environ.get("DEV_MODE", "").lower() in ("dev", "1", "true")
    ts = time.strftime("%Y%m%d-%H%M%S")     # server-run id: stamps browser tabs (?s=)
    # ── RAW SESSION CAPTURE ──────────────────────────────────────────────────
    # ws.jsonseq (every frame in/out, verbatim) + log.jsonseq (every log record) —
    # dumb, complete, untruncated. All interpretation lives OFFLINE in tooling/.
    # See capture.py for the doctrine.
    cap = Capture(model=MODEL, langs=langs, temp=temp, think=think, voice=voice,
                  argv=sys.argv[1:])
    setup_logging(cap, "--verbose" in sys.argv)
    log.info("=== session %s → %s ===", cap.id, cap.dir)
    seed_workspace()

    # playback.wav = the actual send-order stream to the speaker (model post-clamp
    # + CLIPS + earcons) — the ONE audio artifact that never crosses the wire, so
    # it can't be recovered from ws.jsonseq. Mic + model audio ARE in ws.jsonseq
    # (base64), so no separate tutor/mic wavs. (The helper's TRUE rendered output
    # would need a Swift-side tap — deferred.)
    playback_wav = wave.open(str(cap.path("playback.wav")), "wb")
    playback_wav.setnchannels(1); playback_wav.setsampwidth(2); playback_wav.setframerate(OUT_RATE)

    client = genai.Client()
    loop = asyncio.get_running_loop()
    mic_q: asyncio.Queue = asyncio.Queue()
    stopping = threading.Event()
    stop_event = asyncio.Event()
    in_line, out_line = [], []
    thought_line = []                           # this turn's streamed reasoning (Layer 4)
    events = []                                 # the ONE event stream (pending delivery)
    chat_log = []                               # WARNING/ERROR lines headed for the rail
    # the LEVEL strip: mic + speaker dBFS, drained to the browser at 4Hz. A live
    # rough-meter only; the forensic timeline lives in ws.jsonseq/log.jsonseq.
    strip = {"mic": deque(maxlen=400), "out": deque(maxlen=1200)}
    log.addHandler(ChatLogHandler(chat_log))
    jobs = {}                                   # id -> job dict
    js_waiters = {}                             # id -> Future
    stats = {"mic_chunks": 0, "mic_sent": 0, "mic_blocked": 0, "rx_audio_bytes": 0,
             "interrupts": 0, "turns": 0, "reconnects": 0,
             "total_tokens": 0, "jobs": 0, "events_delivered": 0,
             "heartbeats": 0, "rotations": 0, "context_losses": 0,
             "earcons": 0, "screenshots": 0}
    state = {"play_until": 0.0,  # monotonic time the queued playback finishes
             "resume_handle": None, "talking": False, "session": None, "ws": None,
             "stage_path": None, "mic_muted": False, "suspended": False,
             "tool_pending": False, "next_job": 1, "next_js": 1,
             "shutting_down": False, "last_rt_send": 0.0, "rotating": False,
             "rotate_evt": None, "lost_context": False,
             "kbd_colors": {},       # glyph -> css color (teacher-painted, ephemeral)
             "kbd_lang": pick_kbd_lang(langs),
             "browser_told": False,  # what the model last heard about browser presence
             "thinking": False,       # a `thought` part arrived; cleared on audio/turn end
             "greeting_gate": True,   # mic held shut until the opening greeting's first
                                      # audio — protects the fragile pre-audio window from
                                      # noise/event interrupts (field 2026-07-07)
             "quiet_run": 0.0,          # contiguous near-silent model audio queued (tail clamp)
             "tail_clamped_s": 0.0,     # clamped comfort-noise THIS turn → prominent-logged at turn end
             "scrub_turn": False}       # stay_silent tool: scrub ALL of this turn's audio from playback
    kbd_source, kbd_physical, kbd_legends = read_kbd_legends()
    log.info("keyboard: layout=%s, physical=%s, legends from OS input source %r "
             "(%d keys)", state["kbd_lang"], kbd_physical, kbd_source,
             len(kbd_legends))

    def kbd_msg():
        return {"type": "kbd", "lang": state["kbd_lang"],
                "layout": KBD_LAYOUTS[state["kbd_lang"]],
                "physical": kbd_physical,
                "legends": kbd_legends, "colors": state["kbd_colors"]}

    # ── browser link ─────────────────────────────────────────────────────────
    async def ws_send(obj):
        ws = state["ws"]
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_json(obj)
            return True
        except Exception:
            return False

    async def earcon(name):
        # Layer 3 (backend, 2026-07-06): a tiny audio cue for an engine event the
        # developer can't see while listening. Played through the SAME AEC output as
        # the teacher's voice — so it's echo-cancelled (can't trip VAD), it self-
        # records into tutor.wav, and it's LOGGED here. Moved off the browser: the
        # WebAudio version never sounded (AudioContext needs a user gesture a voice-
        # only session never makes). Earcons fire when the teacher is idle/flushed
        # (tool calls block on 3.1; barge flushes first), so queuing is near-instant.
        if not earcons_on:
            return
        pcm = EARCONS.get(name)
        if pcm is None or audio["pipe"] is None:
            return
        stats["earcons"] += 1
        log.info("EARCON: %s", name)
        audio["pipe"].play(pcm)
        try:
            playback_wav.writeframes(pcm)   # the cue lands in the speaker record too
        except Exception:
            pass

    # ── state broadcast: the browser renders ONLY what the server says ───────
    bc_last = {"snap": None}          # reset to None to force a resend

    def state_snapshot():
        running = sum(1 for j in jobs.values() if not j["task"].done())
        busy = running + (1 if state["tool_pending"] else 0)
        link = "ok" if state["session"] is not None else "reconnecting"
        if state["mic_muted"]:
            mic = "muted"
        elif state["suspended"]:
            mic = "away"
        elif ptt:
            mic = "live" if state["talking"] else "ptt-off"
        else:
            mic = "live"                # AEC: open even while the teacher speaks
        now = time.monotonic()
        speaking = now < state["play_until"] + 0.3
        teacher = ("working" if busy else "speaking" if speaking
                   else "thinking" if state["thinking"] else "listening")
        return {"type": "state", "link": link, "mic": mic, "teacher": teacher,
                "busy": busy, "queued": len(events),
                "muted": state["mic_muted"], "away": state["suspended"],
                "off": state["shutting_down"]}

    async def state_broadcaster():
        pipe_dead_reported = False
        while not stop_event.is_set():
            await asyncio.sleep(0.25)
            try:
                # drain the level buffers to the browser
                if strip["mic"] or strip["out"]:
                    payload = {"type": "strip", "now": time.monotonic(),
                               "mic": [[round(t, 3), round(db, 1)]
                                       for t, db in strip["mic"]],
                               "out": [[round(t, 3), round(db, 1), k]
                                       for t, db, k in strip["out"]]}
                    strip["mic"].clear(); strip["out"].clear()
                    await ws_send(payload)
                p = audio["pipe"]
                if (p is not None and not pipe_dead_reported
                        and not stopping.is_set() and p.proc.poll() is not None):
                    pipe_dead_reported = True   # no audio at all — shout, don't limp
                    log.error("aec_helper DIED (rc=%s) — audio is gone; restart "
                              "the app (device change? see its stderr in the log)",
                              p.proc.returncode)
                while chat_log:                    # WARNING/ERROR → chat rail
                    lvl, txt = chat_log[0]
                    if not await ws_send({"type": "logline", "level": lvl,
                                          "text": txt[:300]}):
                        break
                    chat_log.pop(0)
                snap = state_snapshot()
                if snap != bc_last["snap"] and await ws_send(snap):
                    bc_last["snap"] = snap
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("state broadcaster iteration failed")
                await asyncio.sleep(2)

    def enqueue(text, images=None, engage=True, immediate=False):
        """immediate=True → interrupt-class: delivered NOW (flushing any teacher
        speech, bypassing suspend). Default → wait-for-idle. Use immediate only
        when the world has changed out from under the conversation (e.g. ⏻)."""
        events.append({"text": text, "images": images or [], "engage": engage,
                       "immediate": immediate})
        log.info("EVENT queued (engage=%s%s): %s", engage,
                 ", IMMEDIATE" if immediate else "", text)

    # Browser presence is COALESCED before it reaches the model: a reconnect blip
    # (heartbeat cycle, momentary drop) must not spam the event stream. Only a
    # change that stays put ≥1s is worth telling the teacher about.
    browser_evt = {"task": None}

    def notify_browser(present):
        old = browser_evt["task"]
        if old is not None and not old.done():
            old.cancel()

        async def _debounced():
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return
            if present == state["browser_told"]:
                return                         # net no change — say nothing
            state["browser_told"] = present
            enqueue("[the student's browser connected — the stage and keyboard are "
                    "visible to them now]" if present else
                    "[the student's browser disconnected — they can see NOTHING "
                    "(no stage, no keyboard) until it returns]", engage=False)
        browser_evt["task"] = spawn(_debounced(), "notify_browser")

    # ── audio: OS-level AEC via aec_helper (experiments/09) ──────────────────
    # The helper owns both directions: playback we queue becomes the cancellation
    # reference; the mic stream on its stdout has our own audio subtracted, so it
    # stays open while the teacher speaks (voice barge-in via the engine's VAD).
    def on_mic(chunk):                          # reader THREAD of the helper
        if stopping.is_set():
            return
        # live mic level for the strip; the exact mic audio the model hears is on
        # the wire in ws.jsonseq (the realtimeInput frames we send).
        strip["mic"].append((time.monotonic(), pcm_dbfs(chunk)))
        loop.call_soon_threadsafe(mic_q.put_nowait, chunk)

    boot_input_vol = get_input_volume()
    log.info("system input volume at boot: %s", boot_input_vol)

    # The helper is REBINDABLE: while a VPIO session merely EXISTS (even with its
    # audio unit stopped — probed, probe_coclient.py), macOS attenuates every
    # other app's mic capture by ~35dB. ⏸ suspend therefore KILLS the helper
    # (releasing the device for e.g. the user's dictation app) and ▶ respawns it.
    def spawn_pipe():
        return AECPipe(on_mic=on_mic, helper=ensure_aec_helper(),
                       on_log=lambda line: log.info("aec: %s", line))
    audio = {"pipe": spawn_pipe()}

    def queue_play(pcm, kind="model"):
        """Queue PCM on the helper and advance the wall-clock play horizon —
        the helper drains in real time, so 'when playback ends' is arithmetic.
        Also files per-100ms power samples ON THE PLAYBACK CLOCK for the strip
        (chunks arrive in bursts seconds ahead of when they're heard)."""
        if audio["pipe"] is None:              # suspended: student pressed "away"
            log.info("dropping %.1fs of teacher audio (suspended)",
                     len(pcm) / 2 / OUT_RATE)
            return
        audio["pipe"].play(pcm)
        try:
            playback_wav.writeframes(pcm)      # tap: what we actually sent the speaker
        except Exception:
            pass
        now = time.monotonic()
        slot = max(now, state["play_until"])
        step = OUT_RATE // 10 * 2              # 100ms of 24k int16
        for i in range(0, len(pcm), step):
            strip["out"].append((slot + i / 2 / OUT_RATE,
                                 pcm_dbfs(pcm[i:i + step]), kind))
        dur = len(pcm) / 2 / OUT_RATE
        state["play_until"] = slot + dur

    def flush_playback(reason):
        """Drop all queued-but-unplayed audio. Returns seconds dropped."""
        if audio["pipe"] is not None:
            audio["pipe"].flush_playback()
        now = time.monotonic()
        dropped_s = max(0.0, state["play_until"] - now)
        state["play_until"] = now
        log.info("PLAYBACK FLUSHED (%s), dropped %.1fs", reason, dropped_s)
        return dropped_s

    def mic_blocked():
        # the ONLY things that block the mic now: student intent (mute/suspend).
        # The echo gate died with experiments/09 — AEC keeps the mic open.
        return state["mic_muted"] or state["suspended"]

    def model_idle():
        return (time.monotonic() > state["play_until"] + 0.8
                and not state["tool_pending"])

    # ── jobs: receipt + event lifecycle (no double-reporting) ────────────────
    def start_job(code, purpose):
        jid = state["next_job"]; state["next_job"] += 1
        stats["jobs"] += 1
        job = {"code": code, "purpose": purpose, "done_evt": asyncio.Event(),
               "result": None, "receipted": False}

        async def runner():
            try:
                rc, out, err, feeds = await execute_job(code, WORKSPACE, port)
            except asyncio.CancelledError:
                raise
            except Exception:
                # the APP failed, not the teacher's code — never leave a job
                # unreported (the receipt may already promise a [JOB] message)
                log.exception("job %d: executor crashed", jid)
                rc, out, feeds = -2, "", []
                err = ("[internal app error while running this job — not your "
                       "code's fault; details are in the server log]")
            job["result"] = (rc, out, err, feeds)
            job["done_evt"].set()
            await asyncio.sleep(JOB_GRACE + 0.4)
            try:
                head = (f"[JOB {jid} {'DONE' if rc == 0 else f'FAILED rc={rc}'}"
                        + (f" — {purpose}" if purpose else "") + "]")
                if rc != 0 and not job["receipted"]:   # async failure: the ONLY
                    head = (f"⚡ your background job {jid} FAILED: "  # signal it gets
                            f"{one_line_error(err) or f'exit code {rc}'}. Fix it "
                            f"now — re-run corrected code immediately — and tell "
                            f"the student in one breath ('glitch, fixing'). Do "
                            f"this before anything else.")
                lines, images, feed_engage = [head], [], False
                if not job["receipted"]:        # receipt didn't carry the basics
                    out_m, err_m = stash_job_output(jid, out.strip(), err.strip())
                    if out_m:
                        lines.append("stdout: " + out_m)
                    if err_m:
                        lines.append("stderr: " + err_m)
                for f in feeds:
                    if f.get("engage", True):
                        feed_engage = True
                    if f.get("type") == "image" and f.get("path"):
                        try:
                            p = (WORKSPACE / f["path"]).resolve()
                            ok = p.exists()
                        except (OSError, ValueError) as ex:
                            lines.append(f"(fed image path invalid: {f['path']}: {ex})")
                            continue
                        if ok:
                            images.append((p, f.get("caption", "")))
                        else:
                            lines.append(f"(fed image missing: {f['path']})")
                    else:
                        lines.append("fed: " + trunc(str(f.get("text", "")), 1500))
                        if f.get("caption"):
                            lines.append("  ↳ " + f["caption"])
                if job["receipted"] and not feeds:
                    return                      # fully reported in the receipt (even failures)
                # SCHEDULING (emulating the native SILENT/WHEN_IDLE/INTERRUPT that
                # 3.1 can't do — probed 2026-07-05): a FAILED async job INTERRUPTs
                # (⚡); a job that fed something asking for engagement is WHEN_IDLE;
                # a plain successful side-effect job (board/keys/clip staged) is
                # SILENT — deposited as context (turn_complete=False, verified to
                # reach the model) so it does NOT drag the teacher into a spurious
                # spoken turn. stdout is for the teacher's eyes; feed() is the
                # explicit "respond to this" channel.
                failed = rc != 0 and not job["receipted"]
                enqueue("\n".join(lines), images=images,
                        engage=failed or feed_engage, immediate=failed)
            except asyncio.CancelledError:
                raise
            except Exception:
                # never let a finished job vanish unreported
                log.exception("job %d: report assembly crashed", jid)
                enqueue(f"[JOB {jid} finished (rc={rc}) but reporting it hit an "
                        f"internal app error — details in the server log]")

        job["task"] = spawn(runner(), f"job{jid}")
        jobs[jid] = job
        return jid

    # ── event pump: the ONE delivery channel, when idle ──────────────────────
    async def event_pump():
        while not stop_event.is_set():
            await asyncio.sleep(0.4)
            try:
                await pump_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # a bug here must never silently stop ALL event delivery
                log.exception("event pump iteration crashed; continuing")
                await asyncio.sleep(2)

    async def pump_once():
        session = state["session"]
        if not events or session is None:
            return
        if any(e.get("immediate") for e in events):
            flush_playback("interrupt-class event delivery")
        elif state["suspended"] or not model_idle():
            return
        batch, events[:] = events[:], []
        engage = any(e["engage"] for e in batch)
        parts, textbuf, nimg = [], [], 0
        for e in batch:
            textbuf.append(e["text"])
            for p, caption in e["images"]:
                if nimg >= FEED_MAX_IMAGES:
                    textbuf.append(f"(image withheld, cap reached: {p.name})")
                    continue
                try:
                    data = p.read_bytes()
                    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                    if caption:
                        textbuf.append(f"[image: {p.name} — {caption}]")
                    parts.append(types.Part(inline_data=types.Blob(
                        data=data, mime_type=mime)))
                    nimg += 1
                except Exception as ex:
                    textbuf.append(f"(image unreadable: {p.name}: {ex})")
        parts.insert(0, types.Part(text=trunc("\n".join(textbuf), FEED_MAX_TEXT)))
        try:
            await session.send_client_content(
                turns=types.Content(role="user", parts=parts),
                turn_complete=engage)
            stats["events_delivered"] += len(batch)
            log.info("DELIVERED to model (%d event(s), %d image(s), engage=%s): %s",
                     len(batch), nimg, engage, "\n".join(textbuf))
        except Exception:
            log.exception("event delivery failed; requeueing")
            events[:0] = batch
            await asyncio.sleep(2)

    async def graceful_exit():
        """⏻: let the teacher say goodbye and write its notes, then stop.
        The ⏻ event is interrupt-class (delivered within ~1s), but a momentary
        hush right after delivery is NOT done — the model needs time to respond
        (field 00:38: drain fired 0.3s post-delivery; goodbye+notes never ran).
        So: floor of 8s, then require ≥3s of STABLE quiet (no events, no jobs,
        model idle) before tearing down. Hard cap 60s."""
        t0 = time.monotonic()
        quiet = 0
        while time.monotonic() - t0 < 60:
            await asyncio.sleep(0.5)
            busy_jobs = any(not j["task"].done() for j in jobs.values())
            if events or busy_jobs or not model_idle():
                quiet = 0
                continue
            quiet += 1
            if quiet >= 6 and time.monotonic() - t0 > 8:
                break
        log.info("graceful shutdown complete (%.1fs)", time.monotonic() - t0)
        stop_event.set()

    # ── HTTP ─────────────────────────────────────────────────────────────────
    async def index_handler(request):
        # Tab authority = the server-run id `ts`. A BARE `/` (no query at all) is a
        # fresh navigation — hand-typed, bookmark, or our own boot open — so stamp
        # it with THIS run: 302 → /?s=<ts>. Anything already carrying a query keeps
        # it (a reloaded old tab still holds its stale ?s and gets refused at /ws);
        # never redirect a wrong/absent-s query, or a reload would launder itself
        # into the live session. Only an empty query string gets adopted.
        if not request.query_string:
            raise web.HTTPFound(f"/?s={ts}")
        return web.FileResponse(HERE / "static" / "index.html",
                                headers={"Cache-Control": "no-store"})

    async def clip_handler(request):
        text = (request.query.get("text") or "").strip()
        if not text or len(text) > 400:
            return web.Response(status=400, text="need ?text=")
        v = request.query.get("voice") or CLIP_VOICE
        lang = request.query.get("lang") or None
        try:
            path = await ensure_clip(client, text, v, lang)
        except Exception as e:
            log.exception("clip failed")
            return web.Response(status=502, text=str(e))
        return web.FileResponse(path, headers={"Content-Type": "audio/wav",
                                               "Cache-Control": "max-age=86400"})

    async def speak_handler(request):
        d = await request.json()
        text, v = (d.get("text") or "").strip(), d.get("voice") or CLIP_VOICE
        lang = d.get("lang") or None
        if not text:
            return web.json_response({"error": "need text"}, status=400)
        try:
            path = await ensure_clip(client, text, v, lang)
        except Exception as e:
            log.exception("speak: clip generation failed")
            return web.json_response({"error": f"clip generation failed: {e}"},
                                     status=502)
        with wave.open(str(path), "rb") as w:
            pcm = w.readframes(w.getnframes())
        queue_play(pcm, kind="clip")
        return web.json_response({"queued_s": round(len(pcm) / 2 / OUT_RATE, 2)})

    async def warm_handler(request):
        # EAGER clip warming: pre-generate clips in the background so the FIRST
        # student click is instant (not a ~1.5s lazy generation). The warm() helper
        # fires this for a page's click-to-hear words. Cached words return instantly.
        d = await request.json()
        words = [w for w in (d.get("words") or []) if isinstance(w, str) and w.strip()]
        v = d.get("voice") or CLIP_VOICE
        lang = d.get("lang") or None
        for w in words[:40]:
            spawn(ensure_clip(client, w.strip(), v, lang), f"warm:{w.strip()[:20]}")
        return web.json_response({"warming": len(words)})

    # ── peek: the teacher SEES the student's real screen (experiments/…) ──────
    # A screenshot of the FOCUSED window fed back to the teacher's eyes — what the
    # student is actually looking at (the staged page + keyboard, or a wandering
    # YouTube tab). On-demand only, via peek(); the teacher decides when a look is
    # worth it (auto-peek retired 2026-07-08 — softcode over hardcode).
    snap_seq = {"n": 0}

    def capture_screen():
        """Focused-window PNG → workspace/.snapshots. Uses the wincap helper
        (CGWindowListCreateImage on the frontmost real app window): a FLAT bitmap
        of the window's OWN backing store — occlusion-proof and Stage-Manager-proof,
        unlike a full-display grab (which kept catching the desktop/Discord — field
        2026-07-08). Returns Path or None; failure (usually Screen-Recording
        permission, which the wincap binary needs granted once) is logged loudly so
        a blind peek never masquerades as success."""
        import subprocess
        snap_dir = WORKSPACE / ".snapshots"
        snap_dir.mkdir(exist_ok=True)
        snap_seq["n"] += 1
        out = snap_dir / f"snap_{snap_seq['n']:04d}.png"
        try:
            binp = ensure_built("wincap")
        except Exception as e:
            log.error("wincap build failed (%s) — peek is blind", e)
            return None
        try:
            r = subprocess.run([str(binp), str(out)], timeout=8,
                               capture_output=True, text=True)
            if r.returncode != 0:
                log.error("wincap failed (rc=%s: %s) — grant Screen-Recording "
                          "permission (System Settings ▸ Privacy) to the terminal "
                          "running this AND to the wincap binary; peek is blind "
                          "until then", r.returncode, (r.stderr or "").strip()[:200])
                return None
            if r.stdout.strip():
                log.info("peek: captured window [%s]", r.stdout.strip())
            subprocess.run(["sips", "-Z", "1400", str(out)], timeout=8,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError) as e:
            log.error("screenshot capture failed (%s) — peek is blind", e)
            return None
        if not out.exists() or out.stat().st_size == 0:
            log.error("screenshot produced no file — Screen-Recording permission "
                      "is almost certainly denied; peek is blind")
            return None
        return out

    async def feed_screenshot(caption, engage=True):
        # engage=True (2026-07-08): a screenshot is something the teacher asked to
        # SEE and react to — it must WAKE a turn (turn_complete=True). Depositing it
        # as silent context (turn_complete=False) stranded the image until the
        # student next spoke — the "model goes dead" bug. There is no "process but
        # stay silent" in the Live protocol; the prompt keeps the reaction terse.
        # SETTLE (2026-07-09): a stage update is async (show_html → /stage → WS → browser
        # fetch+render), so a peek() right after show_html raced it and captured stale
        # content. Wait 0.5s so the browser has reloaded before capture; log the timing so
        # we can tell a race (stage updated recently) from a genuinely failed update.
        _since_stage = time.monotonic() - state.get("last_stage_t", 0.0)
        log.info("peek: %.2fs since last stage update; settling 0.5s before capture",
                 _since_stage)
        await asyncio.sleep(0.5)
        path = await asyncio.to_thread(capture_screen)
        if path is None:
            log.warning("peek: screen snapshot FAILED (capture returned nothing)")
            enqueue("[⚠️ screen snapshot FAILED — capture returned nothing "
                    "(Screen-Recording permission?); you are blind to the screen]",
                    engage=engage)
            return None
        stats["screenshots"] += 1
        log.info("peek: captured → %s (%s)", path.relative_to(WORKSPACE), caption)
        enqueue(f"[screen snapshot — {caption}]", images=[(path, caption)],
                engage=engage)
        return path

    async def peek_handler(request):
        try:
            d = await request.json()
        except Exception:
            d = {}
        await earcon("shutter")   # the student hears the teacher take a look
        cap = str(d.get("note") or "").strip() or "the student's current screen"
        path = await feed_screenshot(cap, engage=True)
        if path is None:
            return web.json_response(
                {"ok": False, "error": "capture failed (Screen-Recording "
                 "permission?) — see server log"}, status=500)
        return web.json_response(
            {"ok": True, "path": f".snapshots/{path.name}",
             "note": "snapshot queued — it arrives on your event stream shortly"})

    async def stage_handler(request):
        d = await request.json()
        rel = d.get("path", "")
        p = (WORKSPACE / rel).resolve()
        if not str(p).startswith(str(WORKSPACE)) or not p.exists():
            return web.json_response({"error": f"not in workspace: {rel}"}, status=400)
        prev = state["stage_path"]
        replaced = (str(prev.relative_to(WORKSPACE))
                    if prev is not None and prev != p else None)
        state["stage_path"] = p
        state["last_stage_t"] = time.monotonic()   # peek() settle references this
        delivered = await ws_send({"type": "stage"})
        log.info("STAGE <- %s (replaced %s, browser=%s)", rel, replaced, delivered)
        return web.json_response({"ok": True, "showing": rel, "replaced": replaced,
                                  "browser": delivered})

    async def stage_view(_):
        p = state["stage_path"]
        if p is None:
            return web.Response(text=PLACEHOLDER, content_type="text/html")
        if p.suffix.lower() in (".html", ".htm"):
            return web.Response(text=BRIDGE + p.read_text(), content_type="text/html")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            html = (f'{BRIDGE}<!doctype html><html><body style="margin:0;background:#111;'
                    f'display:flex;align-items:center;justify-content:center;height:100vh">'
                    f'<img src="/workspace/{p.relative_to(WORKSPACE)}" '
                    f'style="max-width:96%;max-height:96%"></body></html>')
            return web.Response(text=html, content_type="text/html")
        import html as _h
        return web.Response(text=f"<pre style='color:#e8e8ea;background:#111'>"
                                 f"{_h.escape(p.read_text()[:20000])}</pre>",
                            content_type="text/html")

    async def keyboard_handler(request):
        """Color keys on the student's on-screen keyboard. Mechanism only:
        {'keys': 'йцу', 'color': '#e6c05a'} paints, color=None unpaints,
        {'clear': true} resets all. The widget attaches no meaning to colors.
        VALIDATES against the actual layout, loudly — field 2026-07-04: the
        teacher painted uppercase 'А'/'О' (no such glyphs; keys are lowercase),
        we accepted them silently, nothing lit, and every receipt claimed
        success. A primitive that accepts impossible input manufactures
        confident wrongness downstream."""
        d = await request.json()
        if "layout" in d:                        # switch target-language layout
            code = str(d["layout"]).lower()
            if code not in KBD_LAYOUTS:
                return web.json_response(
                    {"error": f"unknown layout {code!r} — available: "
                              f"{', '.join(sorted(KBD_LAYOUTS))} (RTL/AltGr/CJK "
                              f"layouts not supported yet)"}, status=400)
            if code == state["kbd_lang"]:        # already here → no-op, keep colors
                log.info("KEYBOARD layout already %s (no-op, colors preserved)", code)
                return web.json_response(
                    {"ok": True, "layout": code, "colors": state["kbd_colors"],
                     "note": "already on this layout — colors preserved (a real "
                             "switch to a different alphabet is what clears them)"})
            state["kbd_lang"] = code
            state["kbd_colors"].clear()          # old glyph vocabulary is gone
            await ws_send(kbd_msg())
            log.info("KEYBOARD layout -> %s (colors cleared)", code)
            return web.json_response({"ok": True, "layout": code,
                                      "note": "colors cleared for the new alphabet"})
        valid = {glyph for row in KBD_LAYOUTS[state["kbd_lang"]] for _, glyph in row}
        if "flash" in d:                         # transient glow that decays back
            folded = [ch.lower() for ch in str(d.get("flash") or "") if ch.strip()]
            bad = sorted(set(ch for ch in folded if ch not in valid))
            if bad:
                return web.json_response(
                    {"error": f"no such key(s) on the student's keyboard: {bad} — "
                              f"it has exactly these glyphs (case-insensitive): "
                              f"{''.join(sorted(valid))}"}, status=400)
            delivered = await ws_send({"type": "kbd_flash", "keys": folded,
                                       "color": str(d.get("color") or "#7c9cff"),
                                       "seconds": float(d.get("seconds") or 2.0)})
            return web.json_response({"ok": True, "browser": delivered})
        if d.get("clear"):
            state["kbd_colors"].clear()
        else:
            color = d.get("color")
            folded = [ch.lower() for ch in str(d.get("keys") or "") if ch.strip()]
            bad = sorted(set(ch for ch in folded if ch not in valid))
            if bad:
                return web.json_response(
                    {"error": f"no such key(s) on the student's keyboard: {bad} — "
                              f"it has exactly these glyphs (case-insensitive): "
                              f"{''.join(sorted(valid))}"}, status=400)
            for ch in folded:
                if color:
                    state["kbd_colors"][ch] = str(color)
                else:
                    state["kbd_colors"].pop(ch, None)
        delivered = await ws_send({"type": "kbd_colors",
                                   "colors": state["kbd_colors"]})
        log.info("KEYBOARD colors now: %s (browser=%s)", state["kbd_colors"],
                 delivered)
        return web.json_response({"ok": True, "colors": state["kbd_colors"],
                                  "browser": delivered})

    async def shell_error_handler(request):
        # the SHELL's own runtime errors (window.onerror) — without this, a
        # broken shell is a silent black mystery (field 2026-07-04: dead strip)
        try:
            d = await request.json()
        except Exception:
            d = {}
        txt = d.get("text", "?")
        if txt.startswith("(not an error)"):       # the boot beacon — benign
            log.info("shell: %s", txt)
        else:                                       # a real window.onerror
            log.error("SHELL JS ERROR: %s", txt)
        return web.Response(text="ok")

    async def clientlog_handler(request):
        # client-side timing beacons (WSPROBE) — logged untruncated at INFO so the
        # session log pins where startup time goes. Temporary connection-debug aid.
        try:
            d = await request.json()
        except Exception:
            d = {}
        log.info("CLIENT: %s", d.get("text", "?"))
        return web.Response(text="ok")

    async def js_handler(request):
        d = await request.json()
        if state["ws"] is None or state["ws"].closed:
            return web.json_response({"ok": False, "error": "no browser connected"},
                                     status=409)
        # run_js evaluates INSIDE the staged page's bridge. The boot placeholder has
        # NO bridge, so with nothing staged the eval dead-ends and used to burn 5s of
        # silence before a bare "js timeout" (field 2026-07-06: a 3× doom-loop). Fail
        # FAST and tell the teacher exactly why + the remedy.
        if state["stage_path"] is None:
            return web.json_response({"ok": False, "error":
                "run_js has nothing to run in — no page is staged (the student sees "
                "only the empty placeholder). Stage a page first with show_html(...) "
                "or show(...), or use peek() to see the student's actual screen."},
                status=409)
        jsid = state["next_js"]; state["next_js"] += 1
        fut = loop.create_future()
        js_waiters[jsid] = fut
        await ws_send({"type": "exec_js", "id": jsid, "code": d.get("code", "")})
        try:
            timeout = float(d.get("timeout", 5))
            res = await asyncio.wait_for(fut, timeout=timeout)
            return web.json_response(res)
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error":
                f"run_js timed out after {timeout}s — the staged page never "
                f"answered. Is the code stuck (an infinite loop, or an await that "
                f"never resolves)? Try a simpler expression, or re-show the page."},
                status=504)
        finally:
            js_waiters.pop(jsid, None)

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        # THE SERVER IS THE AUTHORITY. A tab is stamped with the server-run id `ts`
        # (?s=, via the / redirect) and it survives reloads — so a tab from an OLDER
        # run carries a stale `s` and is refused here, deterministically, no matter
        # when it (re)connects. (field 2026-07-05: a load-time "birth" was laundered
        # higher by the client's boot-mismatch reload, so a reloaded OLD tab
        # out-ranked the freshly-opened one. `s` can't be laundered by reload.)
        s = request.query.get("s", "")
        if s != ts:
            try:
                await ws.send_json({"type": "replaced"})   # you're from an old run
            except Exception:
                pass
            await ws.close()
            log.info("refused a tab from another run (s=%r, this run=%r)", s, ts)
            return ws
        if state["ws"] is not None and not state["ws"].closed:
            # a same-run duplicate is taking over; the old holder is TOLD it lost so
            # it retires instead of fighting for the link
            try:
                await state["ws"].send_json({"type": "replaced"})
            except Exception:
                pass
            await state["ws"].close()
        state["ws"] = ws
        log.info("browser connected (s=%s)", s)
        notify_browser(True)
        await ws_send({"type": "status", "mode": "ptt" if ptt else "handsfree",
                       "voice": voice, "langs": langs, "boot": ts})
        await ws_send(kbd_msg())
        await ws_send({"type": "stage"})
        bc_last["snap"] = None                 # force a fresh state broadcast
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            t = d.get("type")
            if t == "event":                       # JS feed() from the stage
                payload = json.dumps(d.get("payload"), ensure_ascii=False)
                enqueue(f"[STAGE EVENT] {payload}", engage=bool(d.get("engage", True)))
            elif t == "jserror":                   # uncaught error in a staged page
                log.warning("stage JS error: %s", d.get("text"))
                enqueue(f"[STAGE JS ERROR — your page has a bug: {d.get('text')} "
                        f"— inspect with run_js or re-show a fixed page]", engage=True)
            elif t == "js_result":
                fut = js_waiters.get(d.get("id"))
                if fut and not fut.done():
                    fut.set_result({"ok": d.get("ok", False),
                                    "result": d.get("result"), "error": d.get("error")})
            elif t == "kbd":                   # on-screen keyboard activity
                if d.get("kind") == "press":
                    # time-delta since the previous keystroke → the teacher can sense
                    # typing evenness (hesitation on a letter = weaker mastery). Omitted
                    # on the first press of a burst (gap too large to be meaningful).
                    _now = time.monotonic()
                    _dt = _now - state.get("last_kbd_t", 0.0)
                    state["last_kbd_t"] = _now
                    _d = f" (+{_dt:.1f}s)" if 0.0 < _dt < 20.0 else ""
                    enqueue(f"[KEYBOARD] student pressed '{d.get('key', '')}'{_d}",
                            engage=False)
                elif d.get("kind") == "submit":
                    state["last_kbd_t"] = 0.0                 # reset burst timing
                    enqueue(f"[KEYBOARD] student typed and submitted: "
                            f"{str(d.get('text', ''))!r}", engage=True)
                elif d.get("kind") == "legend_mismatch":
                    # the OS map and an actual keystroke disagree about a keycap —
                    # almost always means the input source changed mid-session
                    log.warning("keyboard legend mismatch at %s: OS map %r, "
                                "keystroke produced %r — input source changed? "
                                "(restart to re-read legends)", d.get("code"),
                                d.get("expected"), d.get("actual"))
            elif t == "ptt":
                state["talking"] = bool(d.get("talking"))
            elif t == "ui":
                kind = d.get("kind")
                if kind == "interrupt":
                    dropped_s = flush_playback("✋ student")
                    stats["interrupts"] += 1
                    enqueue(f"[✋ the student stopped your audio — ~{dropped_s:.1f}s "
                            f"of your speech went unheard]", engage=False)
                elif kind == "mic":
                    state["mic_muted"] = not d.get("on", True)
                    enqueue(f"[student turned their microphone "
                            f"{'OFF' if state['mic_muted'] else 'ON'}]", engage=False)
                elif kind == "suspend":
                    state["suspended"] = bool(d.get("on"))
                    log.info("suspend %s", "ON" if state["suspended"] else "OFF")
                    if state["suspended"]:
                        # release the AUDIO DEVICE entirely: kill the helper so
                        # macOS drops its voice-processing grip and other apps
                        # (the user's dictation tool) get the mic at full level.
                        # The Gemini connection stays alive via the heartbeat.
                        flush_playback("⏸ suspend")
                        if audio["pipe"] is not None:
                            audio["pipe"].close()
                            audio["pipe"] = None
                            log.info("audio helper KILLED (⏸) — mic device "
                                     "released for other apps")
                        # delivered together with the resume event (pump is
                        # paused while suspended) — still tells the teacher
                        # what the gap in the conversation WAS
                        enqueue("[student suspended the session — attention "
                                "elsewhere]", engage=False)
                    else:
                        if audio["pipe"] is None:
                            audio["pipe"] = spawn_pipe()
                            log.info("audio helper RESPAWNED (▶)")
                        enqueue("[student is back — session resumed]", engage=False)
                elif kind == "shutdown":
                    if not state["shutting_down"]:
                        state["shutting_down"] = True
                        if state["suspended"]:      # ⏻ while away: the goodbye
                            state["suspended"] = False   # must be audible
                            if audio["pipe"] is None:
                                audio["pipe"] = spawn_pipe()
                                log.info("audio helper respawned for ⏻ goodbye")
                        enqueue("[⏻ the student pressed the power button — the session "
                                "is over. Say a brief goodbye and update your notes NOW "
                                "(run_python). The app closes when your work is done.]",
                                engage=True, immediate=True)
                        spawn(graceful_exit(), "graceful_exit")
                elif kind == "visibility":
                    enqueue(f"[browser tab became "
                            f"{'visible' if d.get('visible') else 'hidden'}]", engage=False)
        if state["ws"] is ws:
            state["ws"] = None
            notify_browser(False)
        log.info("browser disconnected")
        return ws

    # ── live session tasks ───────────────────────────────────────────────────
    async def sender(session):
        # the mic (post-AEC) is captured on the wire in ws.jsonseq (realtimeInput
        # frames), so what the model COULD hear is fully recorded there.
        prev_talking = False
        try:
            while True:
                chunk = await mic_q.get()
                stats["mic_chunks"] += 1
                if state["greeting_gate"]:      # opening greeting: keep the mic shut so
                    stats["mic_blocked"] += 1   # ambient noise can't trip the engine's VAD
                    continue                    # and interrupt the greeting before it starts
                if ptt:
                    talking = state["talking"] and not state["suspended"]
                    if talking != prev_talking:
                        if talking:
                            flush_playback("user barge-in (PTT)")
                            await session.send_realtime_input(activity_start=types.ActivityStart())
                        else:
                            await session.send_realtime_input(activity_end=types.ActivityEnd())
                        state["last_rt_send"] = time.monotonic()
                        prev_talking = talking
                    if not talking:
                        stats["mic_blocked"] += 1
                        continue
                elif mic_blocked():
                    stats["mic_blocked"] += 1
                    continue
                stats["mic_sent"] += 1
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={IN_RATE}"))
                state["last_rt_send"] = time.monotonic()
        except Exception as e:
            if is_conn_closed(e):
                log.info("sender: connection closed mid-send (%s)", e)
                return
            raise

    async def heartbeat(session):
        # exp-07: the server kills a connection ~152s after the last client
        # traffic (~50s once audio has flowed) — with NO GoAway. One 100ms
        # silence chunk per HEARTBEAT_S (~5 tokens/min) makes it immortal
        # (verified for auto and manual VAD). Real mic audio resets the clock.
        try:
            while True:
                await asyncio.sleep(2)
                if time.monotonic() - state["last_rt_send"] < HEARTBEAT_S:
                    continue
                await session.send_realtime_input(audio=types.Blob(
                    data=SILENCE_100MS, mime_type=f"audio/pcm;rate={IN_RATE}"))
                state["last_rt_send"] = time.monotonic()
                stats["heartbeats"] += 1
        except Exception as e:
            if is_conn_closed(e):
                log.info("heartbeat: connection closed (%s)", e)
                return
            raise

    async def handle_message(session, msg):
        sc = msg.server_content
        if sc:
            if sc.interrupted:
                stats["interrupts"] += 1
                flush_playback("interrupted")
                await earcon("barge")
            if sc.input_transcription and sc.input_transcription.text:
                in_line.append(sc.input_transcription.text)
                # LOCAL barge-in (field 2026-07-04): the server's `interrupted`
                # only exists while the model is GENERATING. Fully-buffered turns
                # and clips would otherwise play on, deaf to the student — the
                # "uninterruptible other voice" bug. Student speech + queued
                # audio ⇒ flush here ourselves.
                if time.monotonic() < state["play_until"] - 0.3:
                    dropped_s = flush_playback("student spoke over buffered audio")
                    stats["interrupts"] += 1
                    await earcon("barge")
                    enqueue(f"[the student started speaking over your queued "
                            f"audio — ~{dropped_s:.1f}s of it went unheard]",
                            engage=False)
                # live "I hear you" feedback while the student is still speaking
                await ws_send({"type": "partial", "who": "you",
                               "text": "".join(in_line).strip()})
            if sc.output_transcription and sc.output_transcription.text:
                out_line.append(sc.output_transcription.text)
                # (tutor line stays turn-final: its transcript would arrive
                # seconds AHEAD of the audio — spoilers beat listening practice)
            if sc.model_turn:
                for part in sc.model_turn.parts or []:
                    # Layer 4: reasoning tokens (include_thoughts) arrive as text
                    # parts flagged `thought`. They precede the audio — surface
                    # them live so `think:high`'s pre-speech pause reads as a mind
                    # working, not a freeze. UNVERIFIED wire shape (Gemini-web
                    # claim) — handled defensively; a no-op if never emitted.
                    if getattr(part, "thought", False) and getattr(part, "text", None):
                        state["thinking"] = True
                        thought_line.append(part.text)
                        await ws_send({"type": "thought", "text": part.text})
                        continue
                    if part.inline_data and part.inline_data.data:
                        data = part.inline_data.data
                        state["thinking"] = False       # audio → done thinking
                        if state["greeting_gate"]:      # greeting is now audible → open
                            state["greeting_gate"] = False  # the mic (barge-in normal now)
                            log.info("greeting audio started — mic opened")
                        # CLAMP the comfort-noise tail (exp-13, field 2026-07-08): the model
                        # holds its turn open emitting ~-56dB vocoder room-tone (stochastic,
                        # 0.3–57s — an emergent "waiting for you" posture, not a bug). Let
                        # natural pauses through (<=TAIL_GRACE_S, so pacing/counting stay
                        # intact — exp-11) but stop QUEUEING quiet beyond that, so it never
                        # inflates play_until into a phantom backlog. The raw model audio
                        # (incl. any clamped tail) is in ws.jsonseq regardless. >-40 ≈ speech,
                        # <-45 ≈ inaudible tail.
                        _db = pcm_dbfs(data)
                        _dur = len(data) / 2 / OUT_RATE
                        if _db >= -45:                       # speech / audible content
                            state["quiet_run"] = 0.0
                            _clamped = False
                        elif state["quiet_run"] < TAIL_GRACE_S:
                            state["quiet_run"] += _dur       # a natural pause — let it play
                            _clamped = False
                        else:
                            _clamped = True                  # long hold — recorded, not played
                            state["tail_clamped_s"] += _dur
                        _scrub = state.get("scrub_turn", False)   # stay_silent tool
                        if not _clamped and not _scrub:
                            queue_play(data)
                        stats["rx_audio_bytes"] += len(data)
            if sc.turn_complete:
                stats["turns"] += 1
                state["thinking"] = False
                state["quiet_run"] = 0.0
                state["scrub_turn"] = False           # end of the silent turn (stay_silent)
                # PROMINENT tail alarm (exp-14 / forum 174126): with temp>=0.5 the Live
                # "comfort-noise tail" stall should be gone; if we still clamp a real tail,
                # shout so we notice a regression (e.g. temp drifted back toward 0).
                _tail = state["tail_clamped_s"]
                state["tail_clamped_s"] = 0.0
                if _tail >= 2.0:
                    log.warning("⚠️  TAIL DETECTED: clamped %.1fs of comfort-noise tail this "
                                "turn — the low-temp Live stall (forum 174126). temp=%s; if "
                                "this recurs, verify temperature >= 0.5.", _tail, temp)
                you = "".join(in_line).strip()
                tutor = "".join(out_line).strip()
                if thought_line:
                    log.info("THINK: %s", "".join(thought_line).strip())
                    thought_line.clear()
                if in_line:
                    log.info("YOU: %s", you)
                    await ws_send({"type": "transcript", "who": "you", "text": you})
                    in_line.clear()
                if out_line:
                    log.info("TUTOR: %s", tutor)
                    await ws_send({"type": "transcript", "who": "tutor", "text": tutor})
                    out_line.clear()
        if msg.session_resumption_update:
            u = msg.session_resumption_update
            if getattr(u, "resumable", False) and getattr(u, "new_handle", None):
                state["resume_handle"] = u.new_handle
        if msg.usage_metadata and msg.usage_metadata.total_token_count:
            n = msg.usage_metadata.total_token_count
            if n != stats["total_tokens"]:      # the run-up to any 1007 context
                stats["total_tokens"] = n       # overflow must be on record
                log.info("tokens: total=%d", n)
        if getattr(msg, "tool_call_cancellation", None):
            ids = list(getattr(msg.tool_call_cancellation, "ids", []) or [])
            log.warning("TOOL CALL CANCELLED by engine: %s", ids)
        if msg.tool_call:
            # try/finally: tool_pending stuck True would freeze model_idle() and
            # with it ALL event delivery, forever — even across reconnects
            state["tool_pending"] = True
            try:
                await handle_tool_call(session, msg.tool_call)
            finally:
                state["tool_pending"] = False
        if msg.go_away:
            # exp-07: GoAway arrives ~50s before the 10-min rotation kills the
            # connection (frame is sent twice). Rotate proactively at the next
            # idle moment so the cut never lands mid-sentence.
            tl = getattr(msg.go_away, "time_left", None)
            if not state["rotating"]:
                state["rotating"] = True
                log.info("GO_AWAY (time_left=%s) — will rotate when idle", tl)
                await earcon("link")
                await ws_send({"type": "notice", "text": "connection rotating — resuming…"})
                spawn(rotate_when_idle(), "rotate_when_idle")

    async def handle_tool_call(session, tool_call):
        responses = []
        for fc in tool_call.function_calls:
            args = fc.args or {}
            if fc.name == "stay_silent":
                # no-op turn: scrub this turn's audio so the student hears silence. Flush
                # anything already buffered, and scrub_turn skips the rest (model_audio).
                state["scrub_turn"] = True
                flush_playback("stay_silent: scrubbing this turn's audio")
                reason = args.get("reason", "")
                log.info("TOOL stay_silent (%s) — audio scrubbed", reason)
                resp = {"status": "done", "ok": True,
                        "note": "audio scrubbed; the student hears silence this turn"}
            elif fc.name == "run_python":
                await earcon("reach")           # you hear the teacher reach for a tool
                code, purpose = args.get("code", ""), args.get("purpose", "")
                jid = start_job(code, purpose)
                log.info("TOOL run_python (%s): %s", purpose, code)
                job = jobs[jid]
                try:
                    await asyncio.wait_for(job["done_evt"].wait(), JOB_GRACE)
                except asyncio.TimeoutError:
                    pass
                if job["result"] is not None:   # finished within grace
                    rc, out, err, feeds = job["result"]
                    job["receipted"] = True
                    # the model gets the capped copy; the FULL output is on disk in
                    # .jobs/ (stash_job_output) and the RECEIPT log line carries it.
                    out_m, err_m = stash_job_output(jid, out, err)
                    if rc == 0:
                        resp = {"status": "done", "ok": True, "job_id": jid,
                                "exit_code": rc, "stdout": out_m, "stderr": err_m}
                        if feeds:
                            resp["note"] = "fed items arriving in a follow-up message"
                        await earcon("ok")          # ran clean (exit 0)
                    else:
                        # FAILED: hoist the real error to the top, and fire an
                        # immediate ⚡ interrupt — reactive models fall silent on a
                        # passive failure receipt (field 2026-07-04). The event is
                        # a synthetic barge-in that forces the corrective turn.
                        resp = {"status": "failed", "ok": False, "job_id": jid,
                                "error": one_line_error(err) or f"exit code {rc}",
                                "exit_code": rc, "stdout": out_m, "stderr": err_m}
                        enqueue(f"⚡ your last run_python FAILED: "
                                f"{one_line_error(err) or f'exit code {rc}'}. "
                                f"Fix it now — re-run corrected code immediately — "
                                f"and tell the student in one breath (e.g. 'glitch, "
                                f"fixing'). Do this before anything else.",
                                engage=True, immediate=True)
                        await earcon("fail")        # the loud one — exit≠0
                else:
                    resp = {"status": "running", "ok": True, "job_id": jid,
                            "note": "it will report back as a [JOB] message; do "
                                    "not wait for it — stay silent or continue"}
                    await earcon("async")           # went background (slow job)
            else:
                resp = {"status": "failed", "ok": False, "error": "unknown tool"}
            log.info("RECEIPT: %s", str(resp))
            responses.append(types.FunctionResponse(id=fc.id, name=fc.name,
                                                    response=resp))
        await session.send_tool_response(function_responses=responses)

    async def rotate_when_idle():
        """After GoAway: wait (≤35s of the ~50s notice) for the teacher to finish
        speaking, then trigger a planned reconnect via the resume handle."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < 35 and not model_idle():
            await asyncio.sleep(0.5)
        evt = state["rotate_evt"]
        if evt is not None:
            evt.set()

    async def receiver(session):
        # SDK gotcha: one receive() pass == one model turn (research/03).
        try:
            while True:
                saw_turn_end = False
                async for msg in session.receive():
                    if msg.server_content and msg.server_content.turn_complete:
                        saw_turn_end = True
                    await handle_message(session, msg)
                if not saw_turn_end:
                    log.info("receiver: connection closed")
                    return
        except (genai_errors.APIError, ConnectionClosed) as e:
            # exp-07 taxonomy: with the heartbeat + proactive rotation these
            # should be rare, but they are recoverable server-side closes —
            # record and reconnect, don't alarm.
            txt = str(e)
            if txt.startswith("1000"):          # normal closure — usually us
                log.info("receiver: connection closed normally (%s)", txt.strip())
            elif "operation was aborted" in txt:
                log.warning("server idle-killed the connection (heartbeat should "
                            "prevent this — investigate if frequent): %s", txt)
            elif "GoAway" in txt:
                log.warning("rotation deadline beat us to the reconnect: %s", txt)
            elif "1011" in txt or "exhausted" in txt.lower():
                # transient server-side resource/quota blip (free tier: 3 concurrent
                # sessions). Recovers via resume — NOT a real exhaustion (probed:
                # the LLM keeps working). WARNING, not a red ERROR.
                log.warning("transient resource close — recovering via resume "
                            "(free-tier concurrency, not real exhaustion): %s", txt)
            else:
                log.error("receiver: unexpected APIError: %s", txt)
            return

    # ── boot ─────────────────────────────────────────────────────────────────
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/clip", clip_handler)
    app.router.add_post("/speak", speak_handler)
    app.router.add_post("/warm", warm_handler)
    app.router.add_post("/stage", stage_handler)
    app.router.add_get("/stage/view", stage_view)
    app.router.add_post("/js", js_handler)
    app.router.add_post("/shell-error", shell_error_handler)
    app.router.add_post("/clientlog", clientlog_handler)
    app.router.add_post("/keyboard", keyboard_handler)
    app.router.add_post("/peek", peek_handler)
    app.router.add_static("/workspace/", WORKSPACE)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    # Tab policy (field 2026-07-05, server-authoritative): open a fresh tab at a
    # BARE url; `/` 302s it to /?s=<ts> (this run's id). The server holds the
    # session only for a current-run tab and refuses any tab carrying an older
    # `s` at /ws — so a stale tab from a previous run can never steal the link,
    # regardless of connect timing. `s` survives reloads (it's in the URL), which
    # a load-time token did not. NOT "-g": Firefox defers background-opened tabs
    # ~25-30s (probed); foreground-opening loads promptly, and focusing the lesson
    # you just launched is the right UX anyway.
    if sys.platform == "darwin" and "--no-open" not in sys.argv:
        import subprocess
        subprocess.Popen(["open", f"http://127.0.0.1:{port}/"])
        log.info("opened a fresh tab (stamped ?s=%s; older-run tabs are refused)", ts)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    gap = session_gap()
    print(f"● babel — {MODEL} | voice: {voice} | "
          f"{'PTT' if ptt else 'hands-free'} | AEC (interrupt by voice)")
    print(f"● open http://127.0.0.1:{port}   ({gap})")
    print(f"● workspace: {WORKSPACE}\n● capture: {cap.dir}\n")
    log.info("=== start (model=%s ptt=%s voice=%s langs=%s temp=%s think=%s "
             "thoughts=%s earcons=%s dev_mode=%s) ===", MODEL, ptt,
             voice, langs, temp, think, thoughts, earcons_on, dev_mode)

    pump = spawn(event_pump(), "event_pump")
    broadcaster = spawn(state_broadcaster(), "state_broadcaster")
    first_connect = True
    failures = 0                # consecutive quick deaths (any cause)
    try:
        while not stop_event.is_set():
            tasks, waiter, rot_waiter = [], None, None
            rotate_evt = asyncio.Event()
            state["rotate_evt"] = rotate_evt
            state["rotating"] = False
            conn_start = time.monotonic()
            try:
                cfg = build_config(ptt, state["resume_handle"], voice,
                                   langs, temp, think, thoughts)
                async with client.aio.live.connect(
                        model=MODEL, config=cfg) as session:
                    state["session"] = session
                    state["last_rt_send"] = time.monotonic()
                    # tee this connection's wire into ws.jsonseq. MUST be re-applied
                    # on every (re)connect — a rotated session has a fresh _ws.
                    wrap_ws(session, cap)
                    # the setup frame is sent inside connect() (before the tee) —
                    # record its source config so meta.json is self-contained.
                    cap.record_config(cfg)
                    log.info("live session connected (resume=%s)", bool(state["resume_handle"]))
                    if first_connect:
                        first_connect = False
                        # WAIT FOR THE FRONT-END before greeting. The browser-connected
                        # event, delivered ~1s after the browser attaches, otherwise
                        # races INTO the greeting and Gemini interrupts (cancels) its own
                        # generation — the greeting is silently killed (field 2026-07-07,
                        # diagnosed live). So hold the KICK until the browser is up
                        # (headless --no-open: don't wait), then mark the browser as
                        # already-announced so its connect event is SUPPRESSED (the KICK
                        # conveys presence) and can never interrupt the greeting.
                        if "--no-open" not in sys.argv:
                            t_wait = time.monotonic()
                            while (state["ws"] is None
                                   and time.monotonic() - t_wait < 15
                                   and not stop_event.is_set()):
                                await asyncio.sleep(0.1)
                            log.info("front-end %s before KICK",
                                     "ready" if state["ws"] is not None
                                     else "did NOT connect within 15s — kicking anyway")
                        browser_here = state["ws"] is not None
                        state["browser_told"] = browser_here   # suppress the initial
                        browser_note = (                        # [browser connected] event
                            "" if browser_here else
                            "NO BROWSER IS CONNECTED YET — the student can see "
                            "NOTHING (no stage, no keyboard, no board) until one "
                            "connects; you'll get a [browser connected] note. "
                            "Don't refer to anything visual before then. ")
                        kick = (f"[Session start. Local time: "
                                f"{time.strftime('%A %Y-%m-%d %H:%M')}. {gap}. "
                                f"Your workspace files:\n{workspace_listing()}\n"
                                f"{browser_note}"
                                f"The student's stage is EMPTY right now — files "
                                f"like notes.yaml persist, but the SCREEN does not "
                                f"survive an app restart; re-show whatever the "
                                f"lesson needs before referring to it. "
                                f"Read your notes FIRST — ordinary Python: "
                                f"import yaml; print(Path('notes.yaml').read_text()) "
                                f"— then greet the student appropriately.]")
                        log.info("KICK: %s", kick)
                        await session.send_client_content(turns=types.Content(
                            role="user", parts=[types.Part(text=kick)]), turn_complete=True)

                        async def _greet_gate_timeout():
                            await asyncio.sleep(12)      # never lock the mic out forever
                            if state["greeting_gate"]:
                                state["greeting_gate"] = False
                                log.info("greeting gate: 12s timeout, no greeting audio "
                                         "— opening mic")
                        spawn(_greet_gate_timeout(), "greet_gate")
                    elif state["lost_context"]:
                        state["lost_context"] = False
                        stats["context_losses"] += 1
                        kick = (f"[Your connection was reset and your CONVERSATION "
                                f"memory is gone (a session-lifetime limit) — but your "
                                f"workspace is intact. Local time: "
                                f"{time.strftime('%A %Y-%m-%d %H:%M')}. "
                                f"Your workspace files:\n{workspace_listing()}\n"
                                f"Re-read notes.yaml now (ordinary "
                                f"Python), then pick the lesson back up gracefully — "
                                f"briefly acknowledge the hiccup to the student.]")
                        log.info("KICK (context lost): %s", kick)
                        await session.send_client_content(turns=types.Content(
                            role="user", parts=[types.Part(text=kick)]), turn_complete=True)
                    while not mic_q.empty():
                        mic_q.get_nowait()
                    in_line.clear(); out_line.clear()
                    tasks = [spawn(sender(session), "sender"),
                             spawn(receiver(session), "receiver"),
                             spawn(heartbeat(session), "heartbeat")]
                    waiter = spawn(stop_event.wait(), "stop")
                    rot_waiter = spawn(rotate_evt.wait(), "rotate")
                    await asyncio.wait([waiter, rot_waiter, *tasks],
                                       return_when=asyncio.FIRST_COMPLETED)
            except Exception as e:
                # An EXPIRED resume handle fails the connect itself, instantly,
                # and would do so forever (field 15:08) — that explicit signal,
                # and only that signal, warrants dropping the handle and
                # recovering fresh. Any other failure: keep failing loudly with
                # the real error on record (fail fast — no guessing).
                if state["resume_handle"] and "session expired" in str(e).lower():
                    log.warning("resume handle expired — starting FRESH; the "
                                "teacher will be told to re-read its notes")
                    state["resume_handle"] = None
                    state["lost_context"] = True
                log.exception("connection error")
            finally:
                state["session"] = None
                state["rotate_evt"] = None
                allt = tasks + [t for t in (waiter, rot_waiter) if t]
                for t in allt:
                    t.cancel()
                await asyncio.gather(*allt, return_exceptions=True)
            if stop_event.is_set():
                break
            if rotate_evt.is_set():                 # planned GoAway rotation
                stats["rotations"] += 1
                log.info("planned rotation — reconnecting immediately")
                continue
            if time.monotonic() - conn_start > 10:
                failures = 0
            failures += 1
            stats["reconnects"] += 1
            if failures > MAX_RECONNECT:            # never give up mid-lesson:
                log.error("reconnect attempt %d keeps failing — retrying anyway", failures)
                await ws_send({"type": "notice",
                               "text": f"still reconnecting (attempt {failures})…"})
            delay = min(0.5 * (2 ** min(failures, 4)), 8.0)
            await ws_send({"type": "notice", "text": f"reconnecting in {delay:.0f}s…"})
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
    finally:
        print("\n● shutting down…")
        stopping.set()
        pump.cancel()
        broadcaster.cancel()
        for j in jobs.values():
            j["task"].cancel()
        await asyncio.gather(pump, broadcaster, *(j["task"] for j in jobs.values()),
                             return_exceptions=True)
        try:
            if audio["pipe"] is not None:
                audio["pipe"].close()
        except Exception:
            log.exception("aec_helper close error")
        # VPIO used to walk the device-global input gain down (AGC — now off in
        # the helper, but verify-and-repair anyway: other mic apps share it)
        end_vol = get_input_volume()
        if (boot_input_vol is not None and end_vol is not None
                and end_vol != boot_input_vol):
            log.warning("system input volume drifted %s -> %s during the session "
                        "— restoring", boot_input_vol, end_vol)
            set_input_volume(boot_input_vol)
        if state["ws"] is not None and not state["ws"].closed:
            try:                # NUKE the tab: tell it we've shut down so it RETIRES
                await state["ws"].send_json({"type": "bye"})   # instead of retry-
                await state["ws"].close()   # hammering a dead server — that hammering
            except Exception:               # is what feeds Firefox's ws-reconnect
                log.exception("browser ws bye/close failed")   # backoff. Also unblocks
            #                               # runner.cleanup() (it waits on the ws).
        await runner.cleanup()
        try:
            (WORKSPACE / ".last_session").write_text(str(time.time()))
        except OSError:
            log.exception("last_session stamp failed")
        try:
            playback_wav.close()
        except Exception:
            pass
        log.info("STATS: %s", stats)
        # auto-render the readable timeline (best-effort; the processor stays a
        # separate, disposable tool — run before close so the .jsonseq are flushed).
        try:
            import subprocess
            subprocess.run([sys.executable, str(ROOT / "tooling" / "generate_journal.py"),
                            str(cap.dir)], timeout=30, capture_output=True)
            log.info("report → %s/report.txt", cap.dir)
        except Exception:
            log.exception("report generation failed (non-fatal)")
        cap.close()
        print(f"● done. stats: {stats}")
        print(f"● report: {cap.dir}/report.txt")


def main():
    try:
        asyncio.run(selftest() if "--selftest" in sys.argv else run())
    except KeyboardInterrupt:
        print("\n● bye")


if __name__ == "__main__":
    main()
