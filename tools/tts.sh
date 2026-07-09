#!/bin/bash
# tts.sh — text → spoken WAV (Gemini Live as TTS). See tools/_tts.py for usage.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/tools/_tts.py" "$@"
