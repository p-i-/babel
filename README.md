# babel

A voice-first, personalized language tutor with its own memory — working toward the
Young Lady's Illustrated Primer ([VISION.md](VISION.md)).

You talk with an AI teacher in real time. It runs a live lesson: it speaks, listens,
writes on a shared whiteboard it controls, colours an on-screen keyboard in your target
alphabet, plays native-speaker pronunciations you can tap to hear, and keeps its own
notes on what you know — so it picks up where you left off across days.

Built on the **Gemini Live API** (`gemini-3.1-flash-live-preview`), native audio in and out.

## What it does

- **Real-time voice conversation** — barge-in, natural turn-taking, echo-cancelled.
- **A stage it designs** — the teacher writes HTML to a browser "whiteboard" (word cards,
  emoji, recall games) and can **see its own screen** (screenshots) to verify and fix its work.
- **An on-screen keyboard** in the target-language layout, which the teacher colours to
  track your progress (green = mastered, yellow = learning, …).
- **Native-speaker audio clips** — tap any target word to hear it pronounced.
- **Persistent memory** — the teacher reads and writes `notes.yaml` in its own workspace,
  tracking your vocabulary and mastery across sessions.
- **One code tool** — everything the teacher does runs through `run_python` in a persistent
  workspace (plus a `stay_silent` no-op for when it should hold back and let you think).

## Requirements

- **macOS** — the audio stack uses a small Swift Voice-Processing helper, auto-built on start.
- **Python 3.12+**
- **Xcode Command Line Tools** (`xcode-select --install`) — provides `swiftc`.
- A **Gemini API key** (the free tier works): https://aistudio.google.com/apikey

## Quickstart

```bash
git clone <this-repo> && cd babel
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

cp .env.template .env        # then put your GEMINI_API_KEY in .env
./.venv/bin/python webapp/server.py
```

A browser tab opens to the lesson. Grant **Microphone** and **Screen-Recording**
permissions on first run, then say hello.

Useful flags: `--temp <0–1>` (default `0.5`), `--think minimal|low|medium|high`,
`--ptt` (push-to-talk), `--no-earcons`. `DEV_MODE=dev` makes the teacher surface
operational glitches loudly (for development).

## How it's built

- `webapp/server.py` — the async server: Gemini Live WebSocket, the audio pipeline
  (macOS acoustic echo cancellation via a Swift subprocess), the browser bridge, the tool
  loop, and session resumption + context compression for unlimited-length lessons.
- `webapp/workspace/helpers.py` — the standard library the teacher calls from `run_python`
  (stage/board, keyboard, native clips, screenshots, image search). Its docstrings are
  injected into the prompt as the teacher's live API reference.
- `system.md` — the tutor's system prompt: persona, pedagogy, and how it reasons about teaching.
- `webapp/static/` — the browser shell (stage iframe, on-screen keyboard, live-activity strip).

More detail in [`webapp/README.md`](webapp/README.md).

## Notes

- **Secrets live only in the gitignored `.env`** — loaded via `python-dotenv`. Never commit
  `.env`; `.env.template` is the reference.
- Built on **preview** models; behaviour can shift. The tutor logs a prominent `⚠️ TAIL DETECTED`
  warning if it ever hits the low-temperature turn-taking "tail" — keep `--temp ≥ 0.5`.
- Inline `exp-NN` / `research/…` references in code comments point to internal research and
  experiments that are not shipped in this repo.
