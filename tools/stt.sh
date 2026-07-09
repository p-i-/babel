#!/bin/bash
# stt.sh — audio file → transcript/analysis (Gemini). See tools/_stt.py for usage.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/tools/_stt.py" "$@"
