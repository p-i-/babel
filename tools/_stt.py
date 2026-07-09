"""
stt — audio file → transcript/analysis via Gemini (my "ears" for this project).

Usage:
    stt.sh file.wav                       # transcribe + identify language
    stt.sh file.wav "Assess the pronunciation quality of the Ukrainian words."

Accepts wav/aiff/mp3/m4a/flac/ogg. Uses gemini-3.1-flash-lite (500 RPD free tier).
"""
import mimetypes
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

MODEL = "gemini-3.1-flash-lite"
DEFAULT_PROMPT = ("Transcribe this audio exactly (all languages verbatim, native "
                  "script). Then note: language(s) spoken, English translation if not "
                  "English, and anything notable (multiple voices, noise, artifacts).")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    path = Path(sys.argv[1])
    if not path.exists():
        sys.exit(f"no such file: {path}")
    prompt = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PROMPT
    mime = mimetypes.guess_type(str(path))[0] or "audio/wav"
    if path.suffix.lower() in (".aiff", ".aif"):
        mime = "audio/aiff"

    client = genai.Client()
    r = client.models.generate_content(
        model=MODEL,
        contents=[types.Part.from_bytes(data=path.read_bytes(), mime_type=mime),
                  types.Part(text=prompt)])
    print(r.text.strip())


if __name__ == "__main__":
    main()
