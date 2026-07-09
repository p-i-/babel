"""
tts — text → spoken WAV via Gemini Live (the experiment-04 "TTS engine" trick).

Usage:
    tts.sh "Привіт, як справи?" [-o out.wav] [--voice Charon] [--play]

Writes 24 kHz mono 16-bit WAV. Prints the output transcript (self-verification) and
the file path. Exit 1 if the spoken transcript doesn't match the request.
"""
import asyncio
import sys
import tempfile
import wave
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

MODEL = "gemini-3.1-flash-live-preview"
OUT_RATE = 24000
TTS_SYSTEM = """You are a text-to-speech engine, not an assistant.
The user sends text. You speak that text aloud, EXACTLY as written, and NOTHING else.
Never greet, never comment, never confirm, never translate, never add or repeat words.
If the text is a single word, speak just that single word.
Speak clearly at a natural pace, as a native speaker of the text's language."""


def arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum() or c.isspace()).split().__str__()


async def main():
    text = next((a for a in sys.argv[1:] if not a.startswith("-")
                 and sys.argv[sys.argv.index(a) - 1] not in ("-o", "--voice")), None)
    if not text:
        print(__doc__)
        sys.exit(2)
    voice = arg("--voice", "Charon")
    out = Path(arg("-o", tempfile.mktemp(suffix=".wav", prefix="tts_")))

    client = genai.Client()
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=TTS_SYSTEM,
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
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

    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(OUT_RATE)
        w.writeframes(bytes(pcm))
    transcript = "".join(said).strip()
    ok = norm(transcript.strip(" .!?…")) == norm(text.strip(" .!?…"))
    print(f"voice={voice}  said={transcript!r}  match={ok}")
    print(f"wav: {out}  ({len(pcm)/2/OUT_RATE:.2f}s)")
    if "--play" in sys.argv:
        import subprocess
        subprocess.run(["afplay", str(out)])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
