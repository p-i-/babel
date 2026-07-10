# babel webapp — the squeaky-clean core

> *a voice, a stage, an eye, a memory, Python, and one verb.*

The teacher is a Gemini Live voice with **one tool** — `run_python` — executing in a
**persistent per-student workspace**. Everything else is `helpers.py` (seeded once,
then teacher-owned) plus conventions:

- **`feed()` — "print for things"** — in both Python and stage JS: images and annotated
  text flow back to the teacher's eyes on ONE event stream (feeds, job completions,
  student actions, UI events), delivered **when the teacher is idle**.
- **The stage** — an iframe the teacher DESIGNS as HTML by writing workspace files
  (`show_html` / `show_text` / `show`; no fixed board widget — 2026-07-08). Every
  staged page gets the bridge: JS `feed(payload, {engage})` and `speak(text, lang)`
  (native-speaker clips; `warm()` them from Python for an instant first tap).
- **`run_js(code)`** — the escape hatch: inspect/patch the live page from Python.
- **Jobs**: quick code returns in the tool receipt (~1.2s grace); longer code is
  fire-and-forget — `[JOB n DONE]` events arrive by themselves. 30s cap.
- **Memory is teacher-owned**: it keeps its own notes in the workspace; we inject only
  local time, gap-since-last-session, and a file listing at session start.
- **The keyboard** — an always-visible, teacher-colorable on-screen keyboard under
  the stage (target-language layout; physical keys map by position — no OS layout
  needed; Enter submits typed text to the teacher; every press reaches it silently).
  Pure mechanism: `set_keys('йцу', color)` — color semantics live in the prompt.
  Each key shows the student's PHYSICAL keycap small in the corner (read from the
  Mac's active input source at boot via the `kbd_legends` helper — ANSI/ISO-true,
  cross-checked against real keystrokes). Shift (physical or ⇧) types uppercase.
  Nine layouts (de el en es fr it ru tr uk); teacher switches via `set_layout('el')`.
- **Tabs: the server decides (current run wins).** Every boot opens a fresh
  FOREGROUND tab (`--no-open` disables); a bare `/` 302-redirects to `/?s=<run
  id>`, stamping the tab with this server run. The server holds the session only
  for a current-run tab and refuses any tab carrying an older `s` at `/ws` (it
  retires with a 🪦 screen). Because `s` lives in the URL it survives reloads, so
  a stale tab can never out-rank the freshly-opened one — no timing races, no
  ping-pong. (Superseded the load-time "birth" token, which a reload laundered.)
- **The shell reports its own faults**: runtime JS errors and a boot beacon go
  to the server log (`SHELL JS ERROR:`) — a broken shell is never a silent
  black mystery.
- **The level strip** (above the buttons; click ? for the legend): 12 scrolling
  seconds of mic + speaker audio levels (viridis mountains, brighter = louder),
  an engine-state line (green ok / pink working / amber reconnecting / red off /
  grey away), and a red marker when the mic is disabled. A live rough-meter only —
  the full forensic timeline lives in the **session capture** (see below).
- **Audio = OS-level echo cancellation** (`aec_helper`, auto-built from
  `aec_helper.swift` at boot — needs `swiftc`): the mic stays OPEN while the teacher
  speaks, so you **interrupt by voice** — just start talking (server VAD yields the
  floor and flushes playback). No headphones needed; browser clip audio is cancelled
  too (macOS AEC's reference is the whole output device). Design + measurements:
  experiments/09.

## Run

```bash
./.venv/bin/python webapp/server.py --langs en,uk          # hands-free (barge-in)
./.venv/bin/python webapp/server.py --ptt --langs en,uk    # hold-to-talk (SPACE)
# flags: --voice NAME  --port N (default 8642)  --verbose
./.venv/bin/python webapp/server.py --selftest             # E2E: model calls run_python
```

Open **http://127.0.0.1:8642**. Talk any time — including over the teacher.
Buttons: **✋** stop the teacher talking silently (voice does it too; ✋ doubles as a
verbosity signal) · **🎙️** mic on/off · **⏸️** suspend — attention elsewhere; also
RELEASES the microphone device entirely (kills the audio helper; ▶ respawns it
~1s), so other voice apps (dictation etc.) work at full quality while paused ·
**⏻** end session — the teacher says goodbye and WRITES ITS NOTES, then the app
exits cleanly (Ctrl-C remains the instant developer kill — but notes written only
as-you-go survive it). 🧠 pulses while jobs run.
Known edge: output/input device changes mid-session (e.g. plugging AirPods) aren't
followed — restart the app.
Known side-effects while a lesson runs (all machine-scope, all from macOS's
voice-processing stack — exp-09 finding ⑥):
OTHER apps' mic capture is ~35 dB attenuated (use **⏸ to dictate** — it releases
the device) · other apps' audio output is slightly ducked (`.min` is the floor
the OS offers). The AGC input-gain meddling is fixed (off by default +
snapshot/restore).

**DEV_MODE=dev** (in `.env`): the teacher treats operational anomalies as
higher-priority than teaching — surfaces glitches loudly, writes diagnostic files.
Blank it for normal student-facing behavior. Loop design + field findings:
research/10-agent-loop.md.

**Connections are self-healing:** a 30s silence heartbeat defeats the Live API's
~152s idle kill; GoAway triggers a planned rotation at the next idle moment; an
expired resume handle (~2h wall) falls back to a fresh session with a "re-read your
notes" kick. Expected server closes log as WARNING/INFO; ERROR means a real fault
(experiments/07).

## Layout

- `server.py` — live loop · event pump · job runner · `/clip` `/speak` `/stage` `/js`
  `/workspace` endpoints · the bridge.
- `aec_helper.swift` (→ auto-built `aec_helper`) + `aec_pipe.py` — macOS
  Voice-Processing I/O as a subprocess: echo-cancelled 16k mic out, 24k playback in,
  FLUSH for barge-in (experiments/09).
- `static/index.html` — shell chrome (transcripts, buttons, PTT, stage iframe).
- `seed_helpers.py` — pristine copy of the teacher's standard library (seeded to
  `workspace/helpers.py` once; never overwritten).
- `workspace/` — the teacher's home (gitignored): its notes.yaml, staged pages,
  downloaded images. **Delete it to reset the student.**
- `clips/` — pronunciation clip cache (gitignored).
- `capture.py` — the raw session recorder (see below).
- `logs/NNNNNN/` — one folder per session, gitignored (see below).
- `../tooling/generate_journal.py` — renders a session capture into a readable timeline.

## Session capture — the black box

Doctrine: **capture RAW in the live path, interpret OFFLINE.** Every session writes
`webapp/logs/NNNNNN/` (an incrementing `.counter`; delete old ones freely):

- **`ws.jsonseq`** — every WebSocket frame to/from the engine, in+out, **verbatim**
  (mic + model audio ride along as base64). The raw source of truth. RFC-7464 json-seq:
  records delimited by an ASCII Record Separator (`0x1E`, provably absent from JSON),
  so frames stay byte-exact even when Gemini pretty-prints them across newlines.
- **`log.jsonseq`** — every log record, untruncated. Both files stamp each record
  `<wall> <mono>` (both clocks: wall for absolute/cross-process, mono for gap math;
  their divergence would expose a clock step).
- **`meta.json`** — session config INCL. the full `LiveConnectConfig` (system prompt,
  tools, thinking/speech/VAD/compression). The setup frame is sent inside `connect()`
  before the tap, so this is its captured source — the capture is self-contained.
- **`playback.wav`** — the actual speaker mix (model + clips + earcons + tail-clamp);
  the one artifact that never crosses the wire.
- **`report.txt`** — a readable timeline auto-rendered at shutdown: the log narrative +
  audio collapsed to run-summaries + turn-aware stall detection. `snap_NN.png` —
  screenshots pulled out of the wire and referenced (`📸`) inline.

`generate_journal.py <session-dir>` regenerates `report.txt` on demand. The wire
payload schema is documented in `research/vendor-docs/live-api-websockets-reference.md`,
so a future agent can decode `ws.jsonseq` from first principles.

## Validated headless (2026-07-02; AEC integration 2026-07-03)

E2E selftest: model receives single-tool config → calls `run_python(print(6*7))` →
real subprocess executes → receipt returned → model speaks "42". Unit: executor
(stdout/feeds/timeout/errors). HTTP: stage staging + bridge injection, /js 409
without browser, /speak queues cached clip audio, /workspace static.
AEC integration smoke (live 35s boot): helper auto-built, teacher read notes and
greeted aloud on an OPEN mic with zero self-interrupts, understood a spoken reply,
hit its stale board schema and self-repaired, clean SIGTERM teardown.

Evidence for the design: ROADMAP T1 · exp-04 (clips) · exp-05
(teacher vision) · exp-06 (image grid-pick). Prompt: [../system.md](../system.md).
